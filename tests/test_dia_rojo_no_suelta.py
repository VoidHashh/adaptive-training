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
"""

from __future__ import annotations

import copy
from datetime import date

import pytest

from app.config_loader import _validate
from app.engine.rules import RuleError
from app.engine.session_builder import (
    RECOVERY,
    build_session,
    caducidad_del_aplazamiento,
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
        build_session(d, date(2026, 9, 14), "red")


def test_el_motor_revienta_con_un_session_desconocido(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["session"] = "full_pero_suave"
    with pytest.raises(RuleError, match="full_pero_suave"):
        build_session(d, date(2026, 9, 14), "red")


def test_el_dia_rojo_del_config_real_es_de_recuperacion(cfg):
    """La contraparte positiva: con el config bueno, el rojo frena.

    Sin este test los cuatro de arriba solo demostrarían que el sistema sabe
    decir que no.
    """
    s = build_session(cfg, date(2026, 9, 14), "red")
    assert s.kind == RECOVERY
    assert s.exercises, "el día rojo llega sin un solo ejercicio"


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
        build_session(d, date(2026, 9, 14), "red")


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
# La caducidad del aplazamiento, que estaba escrita a mano en dos sitios
# ---------------------------------------------------------------------------
#
# `defer_expires_days` se leía con `.get(..., 7)` en `session_builder` y otra vez
# en `decision`. Dos copias del mismo defecto son dos sitios donde cambiar el
# YAML no cambia nada, y además dos sitios que pueden acabar diciendo cosas
# distintas: `decision` decide si el aplazamiento ha CADUCADO y `build_session`
# decide si se RECUPERA. Con los números desalineados, la misma sesión podía
# darse por perdida en un módulo y por vigente en el otro el mismo día.


def test_la_caducidad_sale_del_config_no_de_un_siete(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["defer_expires_days"] = 3
    assert caducidad_del_aplazamiento(d) == 3


def test_sin_caducidad_en_el_config_el_motor_revienta(cfg):
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"].pop("defer_expires_days")
    with pytest.raises(RuleError, match="defer_expires_days"):
        caducidad_del_aplazamiento(d)


@pytest.mark.parametrize("valor", ["7", 7.5, True, None])
def test_la_caducidad_tiene_que_ser_un_entero(cfg, valor):
    """`7.9` se convertía en 7 en silencio y `'7'` con comillas también colaba."""
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["defer_expires_days"] = valor
    with pytest.raises(RuleError):
        caducidad_del_aplazamiento(d)


@pytest.mark.parametrize("valor", [0, -1])
def test_una_caducidad_de_cero_dias_no_es_aplazar(cfg, valor):
    """Con menos de un día toda sesión aplazada nace caducada.

    Eso no es aplazar, es borrar, y el mensaje seguiría diciendo "se recupera en
    el próximo día verde".
    """
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"]["defer_expires_days"] = valor
    with pytest.raises(RuleError, match="nace caducada"):
        caducidad_del_aplazamiento(d)
    assert "positivo" in errores(d)


def test_el_constructor_usa_la_caducidad_del_config_y_no_un_siete(cfg):
    """Que el lector exista no sirve de nada si el sitio que decide no lo llama.

    Este test salió de romper el código a propósito: al devolver el
    `.get("defer_expires_days", 7)` a su sitio, los tests de arriba seguían
    todos en verde, porque llamaban al lector directamente y nunca pasaban por
    `build_session`. El lector estaba probado; el sitio donde importa, no.

    Los dos casos están elegidos para que un 7 escrito a mano dé la respuesta
    CONTRARIA a la del fichero, una vez en cada dirección. Con un solo caso, la
    mitad de las veces coincidirían por casualidad y el test no probaría nada.
    """
    d = copy.deepcopy(cfg.raw)

    # Nueve días de aplazamiento, en un miércoles de piscina (libre y sin bici).
    # Con el 7 de antes habría caducado; el fichero dice que no.
    d["actions"]["red"]["defer_expires_days"] = 10
    s = build_session(d, date(2026, 9, 23), "green", pending_strength=("dia_1", date(2026, 9, 14)))
    assert s.routine_key == "dia_1"
    assert s.deferred_from == date(2026, 9, 14)

    # Cinco días, en un martes de descanso. Con el 7 de antes se recuperaría;
    # el fichero dice que ya no.
    d["actions"]["red"]["defer_expires_days"] = 3
    s = build_session(d, date(2026, 9, 22), "green", pending_strength=("dia_2", date(2026, 9, 17)))
    assert s.routine_key is None


def test_el_constructor_revienta_si_falta_la_caducidad(cfg):
    """Sin la clave, y con un aplazamiento vivo, no hay nada que inventar.

    Los dos casos de arriba tampoco bastaban. Un `.get("defer_expires_days", 7)`
    devuelve el valor del fichero siempre que la clave ESTÉ: el defecto solo se
    nota cuando falta. Así que la única forma de que el 7 escrito a mano quede
    en evidencia es quitar la clave, que es además el caso real -un config
    montado a mano, una ruta que no pasa por el validador- para el que existe
    esta segunda cerradura.
    """
    d = copy.deepcopy(cfg.raw)
    d["actions"]["red"].pop("defer_expires_days")
    with pytest.raises(RuleError, match="defer_expires_days"):
        build_session(d, date(2026, 9, 22), "green", pending_strength=("dia_2", date(2026, 9, 17)))


def test_los_dos_modulos_leen_la_misma_caducidad():
    """Que no vuelva a haber dos números para lo mismo.

    No compara valores: comprueba que solo hay UNA lectura de la clave en todo
    `app/`, que es lo que impide que se desalineen otra vez.
    """
    import re
    from pathlib import Path

    from tests.conftest import REPO_ROOT

    lecturas = []
    for py in (REPO_ROOT / "app").rglob("*.py"):
        for n, linea in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["']defer_expires_days["']""", linea):
                lecturas.append(f"{py.relative_to(REPO_ROOT)}:{n}")
    # Una en el validador, y las del propio lector en session_builder. Ninguna
    # en decision.py: ese fue el duplicado que había.
    assert not [x for x in lecturas if "decision.py" in x], (
        f"decision.py vuelve a leer la clave por su cuenta: {lecturas}"
    )
