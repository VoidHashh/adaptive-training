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

import copy
import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.engine.decision import ActiveRule, EngineState, apply_execution
from app.engine.signals import DayMetrics
from app.models import Base, Decision, HevyWrite, Notification, WorkoutLog
from app.repository import load_state, save_decision, save_state, upsert_checkin
from app.runner import run_daily, run_reconcile
from tests.conftest import LUNES, dias, sig_completa
from tests.dobles import doble_de, no_es_doble
from app.integrations.hevy import HevyClient
from app.integrations.telegram import TelegramClient

# Con la rotación ya no hay días sin fuerza: cualquier día, si vas, te toca la
# siguiente del ciclo. Lo que decide CUÁL es `EngineState.last_strength`, no el
# día de la semana, así que un test que necesite una rutina concreta pone el
# puntero en la anterior en vez de buscar el día del calendario que la traía.


@contextmanager
def _base_en_blanco():
    """Una base recién creada, sin nada dentro.

    Está fuera de la fixture porque algún test necesita DOS: comparar la misma
    mañana con y sin una respuesta del check-in exige correrlas en bases
    distintas, ya que `run_daily` escribe la decisión y el check-in queda
    guardado. Lo que se compara son dos primeros días, no un día y su secuela.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def db():
    with _base_en_blanco() as session:
        yield session


# ---------------------------------------------------------------------------
# Dobles de los clientes
# ---------------------------------------------------------------------------


@doble_de(HevyClient)
class HevyFalso:
    """El doble del cliente de Hevy.

    `rechaza` ES EL CASO QUE FALTABA, Y ES EL QUE OCURRIÓ. Este doble sabía
    escribir bien (`escribe=True`), no escribir por el interruptor de solo
    lectura (`escribe=False`) y reventar con una excepción (`revienta=True`).
    Lo que no sabía hacer era lo único que pasó de verdad el 2026-09-14:
    CONTESTAR QUE NO. Hevy devolvió un 400, `write_routine` volvió con
    `written=False`, `reason` VACÍO y el motivo en `error`, y ninguna prueba de
    esta casa recorría esa rama. Por eso el fallo de ahí -guardar `reason` en
    vez de `error`, dejando la fila con estado «error» y explicación «»- pudo
    vivir hasta que hizo falta leer la fila.

    Un doble que solo sabe las dos puntas -todo bien y todo roto- no cubre el
    medio, que es donde viven las averías reales.
    """

    def __init__(self, *, revienta: bool = False, escribe: bool = True,
                 con_copia: bool = True, rechaza: int | None = None):
        self.revienta = revienta
        self.escribe = escribe
        # El código con el que Hevy dice que no. `400` significa «el cuerpo
        # estaba mal y no he aplicado nada»; un 5xx significa «no se sabe».
        self.rechaza = rechaza
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

        if self.rechaza is not None:
            # Copiado de lo que devuelve `write_routine` de verdad cuando la API
            # contesta un código que no es 2xx: `reason` se queda VACÍO -ésa es
            # la trampa- y el texto va en `error`, con el código en
            # `http_status`. Si este doble rellenara `reason` por comodidad,
            # el test pasaría con el código viejo y no valdría para nada.
            return WriteResult(
                written=False,
                routine_id=routine_id,
                reason="",
                error=(
                    f"Hevy ha contestado {self.rechaza} y no ha aplicado nada: "
                    '{"error":"Expected string, received array"}'
                ),
                http_status=self.rechaza,
            )
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


@doble_de(TelegramClient)
class TelegramFalso:
    def __init__(self, *, revienta: bool = False):
        self.revienta = revienta
        self.enviados: list[str] = []

    def send(self, text, *, dry_run=False):
        self.enviados.append(text)
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


def test_un_rechazo_de_hevy_guarda_EL_MOTIVO_y_no_una_cadena_vacia(db, cfg):
    """La fila decía «error» y no decía de qué. Es el fallo del 2026-09-14.

    LO QUE PASÓ. El respaldo de las 09:00 mandó el PUT, Hevy contestó 400 -el
    cuerpo llevaba `"notes": []`, un array en un campo de texto- y la fila que
    quedó en `hevy_writes` tenía `status='error'` y `error=''`. El porqué vivía
    en el log del contenedor, que se reconstruyó esa misma tarde y se lo llevó.
    Reconstruir la causa costó una sesión entera y una sonda contra la API de
    verdad, cuando la respuesta había estado ahí y se tiró a la basura.

    LA CAUSA ERA UNA LÍNEA: `res.hevy_reason = r.reason`. En el camino bueno
    `reason` trae el resumen, pero cuando algo va mal `write_routine` lo deja
    vacío a propósito y pone el texto en `error`. O sea que el runner leía el
    campo equivocado exactamente en el único caso en el que ese campo importa.
    Es otra vez la figura de siempre aquí: el valor que se lee no es el valor
    que se usa.

    Y NO ES LO MISMO QUE EL TEST DE ARRIBA. Aquel usa `revienta=True`, que
    levanta una excepción y se recoge en el `except`, donde `hevy_reason` sale
    de `str(exc)` y siempre tuvo texto. Por esa rama el fallo era invisible. La
    que se rompía es ésta: Hevy CONTESTA, contesta que no, y no hay excepción
    ninguna.
    """
    corre(db, cfg, hevy=HevyFalso(rechaza=400), tg=TelegramFalso())
    fila = db.scalars(select(HevyWrite)).first()

    assert fila is not None and fila.status == "error"
    assert fila.error, (
        "la fila dice que falló y no dice por qué: es exactamente lo que quedó "
        "guardado el 2026-09-14 y lo que hizo falta un día entero para "
        "reconstruir"
    )
    assert "Expected string, received array" in fila.error, (
        f"se ha guardado algo, pero no lo que contestó Hevy: {fila.error!r}"
    )


def test_un_rechazo_de_hevy_guarda_EL_CODIGO_que_contesto(db, cfg):
    """`hevy_writes.http_status` existía, estaba documentada, y era siempre NULL.

    POR QUÉ IMPORTA LA COLUMNA. Es la que separa las dos situaciones que se
    parecen en el histórico y piden cosas distintas:

      - 4xx: Hevy ha mirado el cuerpo, lo ha rechazado y NO ha aplicado nada.
        La rutina de allí es la de antes. No hay nada que mirar ni que revertir.
      - 5xx o sin respuesta: no se sabe qué hay en Hevy. Hay que ir a mirar.

    Con la columna a NULL siempre, las dos filas se leen igual -«error»- y la
    única salida es ir a mirar a mano todas las veces, que es lo que hubo que
    hacer. Una columna que nadie rellena no es un dato de menos: es un dato que
    parece existir.
    """
    corre(db, cfg, hevy=HevyFalso(rechaza=400), tg=TelegramFalso())
    fila = db.scalars(select(HevyWrite)).first()

    assert fila is not None
    assert fila.http_status == 400, (
        f"el código con el que Hevy dijo que no se ha perdido: {fila.http_status!r}"
    )


def test_un_rechazo_de_hevy_se_cuenta_en_el_mensaje(db, cfg):
    """Y el motivo no se queda en la base: sale por Telegram esa misma mañana.

    Guardar bien la fila arregla la auditoría de meses después. Lo que arregla
    la mañana es que el aviso salga, porque en Hevy hay la rutina de otro día y
    quien abra la app se la va a encontrar sin saberlo.
    """
    tg = TelegramFalso()
    res = corre(db, cfg, hevy=HevyFalso(rechaza=400), tg=tg)

    assert res.hevy_status == "error"
    assert res.hevy_http == 400
    assert tg.enviados, "Hevy dijo que no y nadie se enteró"
    assert "NO se ha escrito en Hevy" in tg.enviados[0]
    assert any("Expected string, received array" in p for p in res.problemas), (
        f"el motivo no ha llegado a los problemas del día: {res.problemas}"
    )


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
# «Hoy no hago fuerza»: la rutina se escribe en Hevy IGUAL
# ---------------------------------------------------------------------------
#
# EL ÚNICO TROZO DE ESTA PREGUNTA QUE NO VIVE EN EL MOTOR.
#
# Las otras cinco consecuencias de declararlo -no prescribir, no subir carga, no
# mover la rotación, no contar como saltado, contar normal si al final se
# entrena- se comprueban sobre `decide` y `advance_state` en `test_decision` y
# `test_message`, que son funciones puras. Ésta no: escribir en Hevy pasa en
# `runner._escribir_hevy`, después de decidir, y por un camino al que lo
# declarado no llega ni tiene que llegar.
#
# Que no llegue es justamente el motivo de probarlo aquí. El mensaje promete por
# escrito «La rutina está escrita en Hevy de todas formas, por si cambias de
# idea», y hasta ahora lo único que comprobaba esa promesa era un `assert
# "escrita en Hevy" in txt` de `test_message`: o sea, que la frase se dice. Que
# sea verdad no lo miraba nadie. Y es la clase de promesa que se rompe sin hacer
# ruido, porque el día que alguien meta un `if decision.va_a_entrenar is False:
# return` en `_escribir_hevy` -para «ahorrar una llamada a la API», que suena
# razonable- no falla nada: el mensaje sigue llegando, sigue diciendo que la
# rutina está ahí, y quien cambie de idea a las siete de la tarde abre Hevy y
# encuentra la de la semana pasada.
#
# HAY TRES MANERAS DE DECIR «HOY NO TOCA FUERZA» Y LAS TRES VALEN IGUAL.
#
# Contestar que no al «¿vas a entrenar hoy?», elegir «bici» en el selector y
# elegir «otro» son tres frases distintas para el mismo hecho, y ninguna de las
# tres decide si la rutina se escribe. Se parametrizan juntas en vez de probar
# solo la primera porque el `if` que sobra se escribe mirando UN campo: quien
# ponga el atajo en `va_a_entrenar` deja `bici` a salvo por casualidad, y quien
# lo ponga en `chosen_session` deja a salvo el «no voy». Probada una sola, la
# otra mitad del agujero no la ve nadie.
#
# El argumento es del usuario y es el mismo en los tres casos: «si acabo yendo
# al gimnasio, quiero la rutina puesta y no la de hace dos semanas».


# Las tres formas de declarar que hoy no hay fuerza, tal y como llegan del
# formulario. `id` para que el nombre del test diga cuál falló.
SIN_FUERZA = [
    pytest.param({"will_train": False}, id="no_voy"),
    pytest.param({"chosen_session": "bici"}, id="bici"),
    pytest.param({"chosen_session": "otro"}, id="otro"),
]


def _declarando(db, cfg, respuestas, **kw):
    """La mañana de un día en el que el check-in contestó `respuestas`."""
    upsert_checkin(db, LUNES, respuestas, config=cfg)
    return corre(db, cfg, **kw)


@pytest.mark.parametrize("respuestas", SIN_FUERZA)
def test_declarar_que_no_hay_fuerza_no_impide_que_la_rutina_llegue_a_hevy(
    db, cfg, respuestas
):
    """La promesa del mensaje, comprobada contra el hecho y no contra sí misma."""
    hevy, tg = HevyFalso(), TelegramFalso()
    res = _declarando(db, cfg, respuestas, hevy=hevy, tg=tg)

    # Que el montaje ha llegado al motor: sin esto, un día en el que el check-in
    # se perdiera por el camino pasaría el test como si nada, porque un día
    # callado también escribe en Hevy.
    llegado = {
        "will_train": res.decision.va_a_entrenar,
        "chosen_session": res.decision.sesion_elegida,
    }
    assert respuestas.items() <= llegado.items(), (
        f"el montaje no ha llegado al motor: se contestó {respuestas} y en la "
        f"decisión hay {llegado}"
    )

    # Y la fuerza no se prescribe, que es la otra mitad de lo declarado: si esto
    # se cayera, el test de abajo seguiría en verde comparando dos días
    # normales.
    assert res.decision.progression is not None
    assert res.decision.progression.gate_open is False

    assert res.hevy_status == "ok", (
        f"se declaró {respuestas} y la rutina no se ha escrito: "
        f"{res.hevy_status} ({res.hevy_reason})"
    )
    assert hevy.llamadas, "no se ha llamado a Hevy siquiera"

    # Y la fila, que es lo que se lee dentro de tres meses.
    fila = db.scalars(select(HevyWrite)).first()
    assert fila is not None and fila.status == "ok"


def test_la_promesa_de_que_esta_escrita_se_dice_ademas_de_cumplirse(db, cfg):
    """Las dos mitades juntas: se dice Y es verdad.

    Separadas, cada una puede sobrevivir a que la otra se caiga. Va aparte de la
    parametrización de arriba porque la frase hoy solo la dice el camino del «no
    voy a entrenar»; la línea equivalente para `bici` y `otro` es trabajo del
    mensaje y todavía no está escrita. Cuando lo esté, este test se pliega
    dentro del otro.
    """
    tg = TelegramFalso()
    _declarando(db, cfg, {"will_train": False}, hevy=HevyFalso(), tg=tg)
    assert "escrita en Hevy" in tg.enviados[0]


@pytest.mark.parametrize("respuestas", SIN_FUERZA)
def test_la_rutina_que_se_escribe_es_LA_MISMA_diga_lo_que_diga(db, cfg, respuestas):
    """No basta con que se escriba algo: tiene que escribirse lo de siempre.

    Un recorte a medias -escribir la rutina «por si acaso» pero sin los
    ejercicios que el ámbar ya había reducido, o con el peso de ayer en vez del
    de hoy- pasaría el test de arriba entero y dejaría en la aplicación una
    sesión que no es la que el sistema ha decidido. Lo que se declara por la
    mañana es una intención sobre qué se va a hacer; no toca NADA de lo que hay
    que levantar si al final se hace fuerza.

    Se comparan los dos payloads enteros, no el título ni el número de
    ejercicios: cualquier campo que alguien decida podar en el futuro sale aquí.
    """
    declarado = HevyFalso()
    _declarando(db, cfg, respuestas, hevy=declarado, tg=TelegramFalso())

    # El día normal contra el que se compara. En una base APARTE y no en un
    # segundo `corre` sobre la misma: `run_daily` guarda la decisión y el
    # check-in ya está escrito, así que reutilizar `db` compararía la mañana con
    # una versión de sí misma que ya ha pasado.
    with _base_en_blanco() as otra:
        callado = HevyFalso()
        corre(otra, cfg, hevy=callado, tg=TelegramFalso())

    assert declarado.llamadas and callado.llamadas
    assert declarado.llamadas == callado.llamadas, (
        "la rutina escrita en Hevy cambia según lo que se conteste en el "
        "formulario: la pregunta es sobre qué haces hoy, no sobre qué levantas"
    )


def test_elegir_otro_dia_del_ciclo_si_cambia_lo_que_se_escribe(db, cfg):
    """El contraste que le da sentido al test de arriba.

    «La misma rutina diga lo que diga» vale para las tres formas de declarar que
    hoy no hay fuerza, y NO vale para elegir otro día del ciclo: ahí el sistema
    tiene que escribir el Día 2, porque es el que se va a hacer. Sin este test,
    aquella promesa la cumpliría igual de bien un `_escribir_hevy` que ignorase
    el selector por completo, que es exactamente el fallo que el selector viene
    a arreglar.
    """
    elegido = HevyFalso()
    res = _declarando(
        db, cfg, {"chosen_session": "dia_2"}, hevy=elegido, tg=TelegramFalso()
    )
    assert res.decision.rotation_routine == "dia_2"
    assert res.decision.propuesta == "dia_1"

    with _base_en_blanco() as otra:
        callado = HevyFalso()
        corre(otra, cfg, hevy=callado, tg=TelegramFalso())

    assert elegido.llamadas != callado.llamadas, (
        "se eligió el Día 2 y en Hevy ha acabado escrita la misma rutina que un "
        "día callado: el selector no ha llegado a la escritura"
    )


# ---------------------------------------------------------------------------
# La anulación del usuario, desde el envío hasta la fila guardada
# ---------------------------------------------------------------------------
#
# Qué SESIÓN se construye -completa, reducida, recuperación- cuando el usuario
# pide otra de la que el semáforo propone. Las reglas de cuándo se permite y
# cuándo hace falta confirmación están probadas sobre `build_session`, que es
# puro, en `test_dia_rojo_no_suelta.py`. Aquí solo se comprueba una cosa, y es
# la que aquellos tests no pueden ver: que el parámetro LLEGA.
#
# Sin esto, `sesion_pedida` podría existir en las cuatro firmas del camino
# -`run_daily`, `pensar_el_dia`, `decide`, `build_session`- y perderse en
# cualquiera de los tres saltos sin que ningún test se pusiera rojo. Un
# parámetro que nadie pasa es la avería favorita de este proyecto: se declara,
# se documenta, y no hace nada.


def test_la_sesion_pedida_llega_desde_run_daily_hasta_la_fila_guardada(db, cfg):
    """Los tres saltos del camino, comprobados contra lo que queda escrito."""
    from app.engine.session_builder import SesionPedida
    from app.models import Decision as DecisionRow

    res = corre(
        db, cfg, hevy=HevyFalso(), tg=TelegramFalso(),
        sesion_pedida=SesionPedida("recovery", motivo="vengo reventado"),
    )

    assert res.decision.session.kind == "recovery"
    fila = db.scalars(select(DecisionRow)).first()
    anulacion = json.loads(fila.planned_session_json).get("anulacion")
    assert anulacion, (
        "la sesión se ha anulado y la fila guardada no lo dice: dentro de tres "
        "meses ese día se lee como una recuperación que decidió el sistema"
    )
    assert anulacion["pedida"] == "recovery"
    assert anulacion["motivo"] == "vengo reventado"
    # Y la contraparte: la propuesta que se anuló sigue escrita. Sin ella no se
    # puede medir en qué DIRECCIÓN discrepa el usuario, que es una de las tres
    # cosas que hay que poder preguntarle a este histórico.
    assert anulacion["propuesta"] == "full"


def test_un_dia_sin_anular_nada_no_deja_marca_de_anulacion(db, cfg):
    """El contraste. Si la marca saliera siempre, no distinguiría nada.

    Es el mismo argumento que `test_pedir_exactamente_lo_propuesto_no_es_una_
    anulacion`, pero sobre la fila guardada: lo que se mide más adelante se
    cuenta de aquí, y un campo que está lleno todos los días da un 100% de
    desacuerdo y no dice nada de nadie.
    """
    from app.models import Decision as DecisionRow

    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    fila = db.scalars(select(DecisionRow)).first()
    assert json.loads(fila.planned_session_json).get("anulacion") is None


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


# ---------------------------------------------------------------------------
# El wellness que se SABE, no solo el que se acaba de leer
#
# `metrics` es la ventana corta de Garmin: `baseline.window_days + 1`, ocho días
# con el config de hoy. Es pequeña a propósito, porque el wellness se pide día a
# día y cada día son varias llamadas. Lo que estaba mal era decidir con ella:
# `daily_metrics` llevaba seis meses escritos y nadie los abría para decidir.
# ---------------------------------------------------------------------------


def _wellness_en_la_base(db, day, n, **kw):
    """`n` días de wellness guardados terminando en `day`, ambos incluidos."""
    from app.models import DailyMetrics

    for i in range(n):
        db.add(DailyMetrics(date=day - timedelta(days=i), **kw))
    db.flush()


def test_la_ventana_larga_de_la_tendencia_manda_sobre_la_de_las_bases(cfg):
    """Con el config real gana el cualificador de sueño: 90 días.

    No es un número elegido, es el máximo de dos exigencias que vienen cada una
    de su sitio. Si mañana la ventana larga se acorta por debajo de 21, mandaría
    la otra; el test de abajo comprueba ese lado.
    """
    from app.runner import dias_de_wellness_en_memoria

    assert dias_de_wellness_en_memoria(cfg) == cfg.raw["trend"]["ventana_larga_dias"]
    assert dias_de_wellness_en_memoria(cfg) == 90


def test_sin_capa_de_tendencia_siguen_haciendo_falta_las_lineas_base(cfg_copia):
    """Apagar la tendencia no devuelve el sistema a los ocho días.

    `build_signals` reconstruye catorce días de derivadas, y la base del más
    viejo mira los siete ANTERIORES a él. Ese suelo no lo pone la tendencia.
    """
    from app.engine.signals import DIAS_DE_HISTORIA
    from app.runner import dias_de_wellness_en_memoria

    cfg_copia.raw["trend"]["enabled"] = False
    esperado = DIAS_DE_HISTORIA + cfg_copia.raw["baseline"]["window_days"]
    assert dias_de_wellness_en_memoria(cfg_copia) == esperado
    assert esperado == 21


def test_la_mañana_decide_con_lo_guardado_y_no_solo_con_lo_leido(db, cfg, monkeypatch):
    """Que el motor reciba la memoria, no la ventana de la mañana.

    La avería medida: la línea base de AYER mira los siete días anteriores a
    ayer, y el séptimo caía fuera de la ventana corta. La media salía sobre seis
    días, por encima del mínimo, sin nota y sin error. Eso mueve `hrv_ratio` de
    ayer, que es lo que lee `consecutive_days: 2`. Sobre los 167 días del
    histórico el color cambiaba en cuatro: un rojo de verdad perdido el 19/04 y
    tres rojos falsos el 31/03, el 11/04 y el 22/06.
    """
    visto: dict[str, object] = {}
    real = run_daily.__globals__["build_signals"]

    def espia(config, day, *, metrics, **kw):
        visto["metrics"] = list(metrics)
        return real(config, day, metrics=metrics, **kw)

    monkeypatch.setitem(run_daily.__globals__, "build_signals", espia)

    _wellness_en_la_base(db, LUNES - timedelta(days=8), 60, hrv=61.0, rhr=49.0)
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())

    fechas = {m.date for m in visto["metrics"]}
    assert LUNES - timedelta(days=8) in fechas, (
        "la línea base de ayer se sigue calculando sobre seis días"
    )
    assert len(fechas) > len(metricas()), "solo ha llegado la ventana de Garmin"


def test_la_serie_de_sueño_de_la_tendencia_llega_al_mes_de_referencia(
    db, cfg, monkeypatch
):
    """El cualificador de sueño lleva 185 días mudo, y culpando al dato.

    Compara la media de los últimos 30 días contra la de los 60 anteriores. Con
    ocho días delante no llegaba ni a la cobertura mínima, así que contestaba
    «no hay serie suficiente» todas las mañanas con la serie entera en la base.
    """
    visto: dict[str, object] = {}
    real = run_daily.__globals__["evaluar_tendencia"]

    # La firma va explícita y no con `**kwargs` a propósito: cuando el 24/09/2026
    # se le añadió `hrv=` a la llamada real, este doble reventó con un TypeError
    # y obligó a mirar. Con `**kwargs` habría seguido verde espiando una llamada
    # que ya no era la que hace producción, que es la clase de doble que
    # certifica lo que no ha visto.
    def espia(config, day, serie, *, sleep_score, sleep_min, hrv):
        visto["sleep_min"] = dict(sleep_min)
        return real(
            config, day, serie,
            sleep_score=sleep_score, sleep_min=sleep_min, hrv=hrv,
        )

    monkeypatch.setitem(run_daily.__globals__, "evaluar_tendencia", espia)

    larga = cfg.raw["trend"]["ventana_larga_dias"]
    _wellness_en_la_base(db, LUNES, larga, sleep_min=430, sleep_score=78)
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())

    serie = visto["sleep_min"]
    vistos = sum(
        1 for i in range(larga) if serie.get(LUNES - timedelta(days=i)) is not None
    )
    assert vistos == larga, f"solo llegan {vistos} de los {larga} días que compara"


def test_archivar_sigue_guardando_solo_lo_que_garmin_acaba_de_contestar(db, cfg):
    """Lo fusionado se DECIDE con ello, no se vuelve a escribir.

    Guardarlo reescribiría noventa filas cada mañana y les recalcularía el
    `fetch_status`. Un día marcado `error` -no se pudo leer, hay que volver- se
    convertiría en `partial` -se leyó y no había, no se vuelve-, y el backfill
    dejaría de reintentarlo. La marca desaparece justo en los días que la
    necesitan.
    """
    from app.models import DailyMetrics

    viejo = LUNES - timedelta(days=40)
    db.add(DailyMetrics(date=viejo, fetch_status="error", fetch_error="502 de Garmin"))
    db.flush()

    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())

    fila = db.scalars(select(DailyMetrics).where(DailyMetrics.date == viejo)).first()
    assert fila.fetch_status == "error", "archivar la fusión ha borrado el reintento"
    assert fila.fetch_error == "502 de Garmin"


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
    plan: dict, wid: str = "w1", day: date = LUNES, factor_peso: float = 1.0,
    *, sin_plan: bool = False,
) -> dict:
    """Un entrenamiento que cumple el plan entero, construido DESDE el plan.

    `weight_kg` viaja, y no es un detalle de fidelidad: el cumplimiento compara
    el peso, así que un entrenamiento de mentira sin pesos no es "el plan hecho
    entero", es el plan hecho a cero kilos. Cuando esto no lo copiaba, seis
    semanas de sesiones perfectas no subían ni un kilo y el test lo cantaba.

    `factor_peso` sirve para el caso contrario: 0.8 son las mismas reps con menos
    peso, que es exactamente la sesión que antes se colaba como limpia.

    `sin_plan=True` ES OBLIGATORIO PARA LOS ENTRENAMIENTOS SUELTOS, Y ÉSE ES
    TODO EL PUNTO
    ---------------------------------------------------------------------------
    Esta función lee `plan["exercises"]` y `plan["hevy_routine_id"]` con `.get`,
    y el plan que recibe viene de `planned_session()`, que devuelve lo que el
    motor serializó ese día. O sea: la forma del diccionario la decide código
    que está a cuatro ficheros de aquí y puede cambiar sin que nadie mire este
    módulo.

    Si `exercises` deja de llamarse así, esto devuelve un entrenamiento de CERO
    ejercicios. Y un entrenamiento de cero ejercicios cumple cualquier cosa que
    se le pida: `run_reconcile` lo puntúa como sesión perfecta, las rachas
    avanzan, y `test_reconciliar_avanza_la_racha` sigue verde sin haber
    ejecutado nada. El peor es
    `test_reconciliar_dos_veces_no_cuenta_dos_veces`, cuya afirmación central es
    `segunda == primera`: con las dos mitades vacías eso es `{} == {}`, verde
    para siempre.

    Si lo que desaparece es `hevy_routine_id`, el `routine_id` sale a `None`,
    que NO es una etiqueta que falta: es la etiqueta de «entrenamiento suelto».
    Eso ya pasó una vez y está contado tres párrafos más abajo.

    Trece llamadas de este módulo pasan `{"exercises": []}` a propósito -son los
    tests del entrenamiento por libre-, así que el aviso no puede ser
    incondicional. Tiene que ser una declaración: quien quiera un entrenamiento
    sin plan lo dice, y quien no lo diga y se quede sin plan es que algo se ha
    roto.
    """
    assert sin_plan or plan.get("exercises"), (
        f"plan sin ejercicios para {wid}: un entrenamiento vacío cumple "
        "cualquier cosa que se le compare y el test no mediría nada. Si el "
        "entrenamiento suelto es lo que se busca, pásalo con `sin_plan=True`."
    )
    assert sin_plan or plan.get("hevy_routine_id"), (
        f"plan sin `hevy_routine_id` para {wid}: `routine_id` saldría a None, "
        "que es la etiqueta de «entrenamiento suelto», y la sesión se "
        "registraría como hecha por libre haciendo exactamente lo que el plan "
        "pedía. Si eso es lo que se busca, pásalo con `sin_plan=True`."
    )
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
        # `routine_id`, Y ES EL CAMPO QUE HACE QUE ESTO SEA UNA SESIÓN DEL PLAN
        # ----------------------------------------------------------------------
        # Sin esta línea, `routine_key_de` devolvía `None` para todos los
        # entrenamientos de mentira de este módulo, y `None` no es una etiqueta
        # que falte: es la etiqueta de «entrenamiento suelto». Consecuencias
        # medidas, no supuestas, sobre la simulación de seis semanas:
        #
        #   - `WorkoutLog.routine_key` quedaba a NULL en las 42 filas, y el
        #     filtro de rotación de `repository.py` -`routine_key.in_(...)`- no
        #     encuentra NULL nunca, así que `state.last_strength` no se movía y
        #     la rotación repetía `dia_1` los 42 días. Las seis semanas eran el
        #     día 1 cuarenta y dos veces.
        #   - las 42 filas se guardaban con `unplanned=True`, o sea que el doble
        #     hacía exactamente lo que el plan pedía y el sistema lo anotaba como
        #     hecho por libre.
        #   - `all_sets_at_target` quedaba a NULL en las 42, porque solo se le
        #     pone a la fila que sale de la rutina de fuerza del día.
        #
        # El plan lleva el id de Hevy de su rutina, así que se copia de ahí y no
        # se inventa: un id a mano volvería a mentir en cuanto el `config.yaml`
        # cambiara. Cuando el plan no tiene rutina -los `{"exercises": []}` de
        # los tests de entrenamiento suelto- sale `None`, que ahí SÍ es lo que
        # se quiere decir.
        "routine_id": plan.get("hevy_routine_id"),
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
    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="sin_plan")
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


def _ejecuta(media: dict, *, wid: str, rid: str) -> dict:
    """Un entrenamiento que ejecuta ENTERO el plan que se le pase.

    ANTES ESTO PARTÍA UNA LISTA, Y POR AHÍ SE QUEDÓ CIEGO. Cuando el HIIT
    viajaba dentro de `plan["exercises"]`, la forma de fabricar los dos
    entrenamientos era filtrar esa lista por las claves del bloque. Al sacar el
    HIIT a `plan["hiit"]` el filtro siguió compilando y siguió pasando: la mitad
    "dentro" salía vacía, y un entrenamiento sin ejercicios cumple cualquier
    cosa que se le pida. Los tests del HIIT pasaban sin medir nada.

    Con las dos mitades ya separadas en el plan no hay nada que filtrar, así que
    la función ya no puede quedarse callada: si le dan un plan vacío, revienta.

    El `rid` se le da a `_entrenamiento_completo` DENTRO del plan en vez de
    pegarlo encima del resultado. Es la misma idea una capa más abajo: parcheado
    después, la comprobación de que el plan trae rutina no vería nunca este
    camino, y esta función volvería a ser la única que sabe que aquí hace falta
    un `routine_id`.
    """
    assert media.get("exercises"), (
        f"plan vacío para {wid}: un entrenamiento sin ejercicios cumple "
        "cualquier cosa y el test no comprobaría nada"
    )
    return _entrenamiento_completo(
        {"exercises": media["exercises"], "hevy_routine_id": rid}, wid=wid
    )


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

    fuerza = _ejecuta(plan, wid="fuerza", rid=_rid(cfg_lunes, "dia_1"))
    hiit = _ejecuta(plan["hiit"], wid="hiit", rid=_rid(cfg_lunes, "hiit_dia_1"))

    res = run_reconcile(db, cfg_lunes, LUNES, workouts=[fuerza, hiit])

    assert res.sueltos == [], f"lo que el plan pedía sale como fuera del plan: {res.sueltos}"
    filas = {f.hevy_workout_id: f for f in db.scalars(select(WorkoutLog)).all()}
    assert set(filas) == {"fuerza", "hiit"}
    assert filas["hiit"].routine_key == "hiit_dia_1"
    assert filas["hiit"].unplanned is False
    assert filas["hiit"].all_sets_at_target is True, (
        "el HIIT se hizo entero; ahora tiene veredicto PROPIO, no el de la fuerza"
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

    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="hiit_suelto")
    w["routine_id"] = _rid(cfg, "hiit_dia_1")

    res = run_reconcile(db, cfg, LUNES, workouts=[w])

    assert [s["routine"] for s in res.sueltos] == ["hiit_dia_1"]
    fila = db.scalars(select(WorkoutLog)).one()
    assert fila.unplanned is True
    assert "HIIT por libre" in fila.motivo_suelto, fila.motivo_suelto


def test_el_hiit_que_pedia_el_plan_se_nombra_por_su_titulo(db, cfg_lunes):
    """Este motivo lo lee el mensaje de la mañana tal cual sale de aquí, así
    que un `hiit_dia_1` guardado en la fila es un `hiit_dia_1` en pantalla.

    El escenario es el que hace visible la frase: el plan pedía un bloque HIIT y
    se ejecutó OTRO. Hacer el que tocaba no pasa por aquí -se reconoce antes
    como previsto-, así que sin esta discordancia la rama que nombra el bloque
    del plan no la comprueba nadie.

    EL SEGUNDO BLOQUE SE FABRICA AQUÍ, Y ESO ES UN ARREGLO
    ------------------------------------------------------
    Antes se usaba `hiit_dia_2`, que existía en el `config.yaml`. El 25/09/2026
    ese bloque se fusionó dentro del Día 2 y dejó de existir, y con él se llevó
    este test por delante: el escenario dependía de que el config real tuviera
    DOS bloques HIIT.

    Eso era la debilidad, no la fusión. La rama que se prueba aquí es correcta y
    sigue estándolo; lo que pasa es que con un solo bloque en el YAML no se
    puede llegar a ella desde el config real. Fabricando el segundo aquí, el
    test cubre la rama pase lo que pase con la configuración, que es lo que
    tenía que haber hecho desde el principio.
    """
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())
    assert _plan_guardado(db)["hiit_block"] == "hiit_dia_1", (
        "el escenario ya no lleva HIIT; el test hay que rehacerlo"
    )

    cfg_lunes.raw["routines"]["hiit_otro"] = {
        "title": "Otro HIIT",
        "hevy_routine_id": "rid-hiit-otro",
        "exercises": [],
    }
    cfg_lunes.raw["hiit"]["blocks"]["dia_2"] = "hiit_otro"

    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="el_otro_hiit")
    w["routine_id"] = _rid(cfg_lunes, "hiit_otro")
    run_reconcile(db, cfg_lunes, LUNES, workouts=[w])

    fila = db.scalars(
        select(WorkoutLog).where(WorkoutLog.hevy_workout_id == "el_otro_hiit")
    ).one()
    assert fila.motivo_suelto == "HIIT por libre: ese día el plan pedía Día 1 HIIT", (
        fila.motivo_suelto
    )


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

    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="rojo", day=LUNES)
    res = run_reconcile(db, cfg, LUNES, workouts=[w])

    assert not res.avanzado
    assert res.workouts_nuevos == 1
    fila = db.scalars(select(WorkoutLog)).one()
    assert fila.unplanned is True
    assert fila.date == LUNES


# ---------------------------------------------------------------------------
# Declaré una rutina y en Hevy acabé registrando otra
# ---------------------------------------------------------------------------
#
# ESTE CASO YA SE DETECTABA. LO QUE FALTABA ERA LO ÚNICO QUE TIENE CONSECUENCIAS.
#
# Declarar el Día 2 y abrir el Día 1 en el gimnasio ya salía como «visto fuera
# del plan» con el motivo «es dia_1 y en la rotación tocaba dia_2». Eso cuenta
# el desajuste y se queda corto en lo que de verdad cambió: los pesos.
#
# Lo que hay dentro de una rutina de Hevy es lo que el motor escribió la última
# vez que ESA rutina se planificó, porque entre medias no la toca nadie. El
# semáforo de esa mañana, la progresión de esa mañana y el recorte del ámbar
# fueron a la otra. Sin decirlo, una sesión que se hace cuesta arriba porque
# arrastra la carga de hace dos semanas se lee como un mal día.
#
# La alternativa era escribir las tres rutinas cada mañana. Se descartó: no
# compensa triplicar las llamadas a la API por un caso raro, y el caso raro deja
# de doler en cuanto se nombra.


def _escrita_el(db, rkey: str, dia: date, estado: str = "ok") -> None:
    """La última mañana en que el sistema puso pesos en esa rutina de Hevy."""
    db.add(
        HevyWrite(date=dia, routine_key=rkey, hevy_routine_id=_rid_de(rkey), status=estado)
    )
    db.flush()


def _rid_de(rkey: str) -> str:
    return f"rid-{rkey}"


def _declaro_y_entreno(db, cfg, *, declara, ejecuta, pesos_de=None, estado_pesos="ok"):
    """El check-in declara una, el motor escribe la declarada, y en Hevy sale otra.

    Va por `run_daily` entero y no por `_motivo_suelto` a mano, que sería mucho
    más corto: lo que se está comprobando es un CABLE -que la elección del
    check-in llegue hasta la frase de la noche siguiente-, y un test que llamara
    al helper con los argumentos ya resueltos pasaría igual el día que alguien
    deje de pasárselos.
    """
    if declara is not None:
        upsert_checkin(db, LUNES, {"chosen_session": declara}, config=cfg)
    if pesos_de is not None:
        _escrita_el(db, ejecuta, pesos_de, estado_pesos)

    res = corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    planificada = _plan_guardado(db).get("routine")

    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="otro_dia")
    w["routine_id"] = _rid(cfg, ejecuta)
    run_reconcile(db, cfg, LUNES, workouts=[w])

    fila = db.scalars(
        select(WorkoutLog).where(WorkoutLog.hevy_workout_id == "otro_dia")
    ).one()
    return res, planificada, fila


def test_lo_declarado_y_lo_entrenado_salen_con_la_fecha_de_los_pesos(db, cfg):
    """La frase entera, que es el encargo literal.

    Tres hechos y ni uno más: qué declaré, qué entrené, y de cuándo eran los
    pesos que llevaba dentro lo que entrené. El tercero es el que no se podía
    deducir mirando la fila, y es el que explica por qué la sesión fue como fue.

    Se comprueban por TÍTULO -«Día 1», no `dia_1`- porque esta cadena se guarda
    para que el mensaje de la mañana la lea tal cual. Un identificador crudo en
    la pantalla del móvil obliga a traducirlo mentalmente a las nueve de la
    mañana, que es cuando menos ganas hay.
    """
    hace_doce = LUNES - timedelta(days=12)
    _, planificada, fila = _declaro_y_entreno(
        db, cfg, declara="dia_2", ejecuta="dia_1", pesos_de=hace_doce
    )
    assert planificada == "dia_2", f"el montaje no ha llegado al plan: {planificada}"

    assert fila.unplanned is True
    assert fila.motivo_suelto == (
        f"declaraste Día 2 y entrenaste Día 1, con los pesos del "
        f"{hace_doce.strftime('%d/%m')}: los ajustes de la mañana fueron a Día 2"
    ), fila.motivo_suelto


def test_sin_selector_la_frase_dice_tocaba_y_no_declaraste(db, cfg):
    """Atribuirle a la rotación una elección mía, o al revés, es contarme mal mi día.

    Son dos frases y no una porque describen dos cosas distintas: «declaraste
    Día 2» solo es verdad si contesté el selector, y llamar declaración a lo que
    propuso el ciclo un día en que no rellené el formulario convierte mi silencio
    en una afirmación. La diferencia importa para lo que se pidió registrar:
    saltarse el Día 1 varias veces seguidas es información sobre mí, y solo lo es
    si lo que se cuenta son elecciones y no propuestas.
    """
    hace_doce = LUNES - timedelta(days=12)
    _, planificada, fila = _declaro_y_entreno(
        db, cfg, declara=None, ejecuta="dia_2", pesos_de=hace_doce
    )
    assert planificada == "dia_1", (
        f"sin check-in la propuesta debería ser la primera del ciclo: {planificada}"
    )

    assert fila.motivo_suelto.startswith("tocaba Día 1 y entrenaste Día 2"), (
        fila.motivo_suelto
    )
    assert "declaraste" not in fila.motivo_suelto, fila.motivo_suelto


def test_declarar_bici_no_convierte_la_propuesta_en_una_declaracion(db, cfg):
    """«Bici» es declarar que no hay fuerza, no declarar qué fuerza.

    El selector tiene cinco opciones y solo tres son rutinas. La rutina que se
    escribió ese día la siguió eligiendo la rotación -por decisión del usuario:
    si acabo yendo al gimnasio quiero la rutina puesta, no la de hace dos
    semanas-, así que «declaraste Día 1» sería falso. Escrito sin filtrar el
    selector contra el ciclo, este caso diría exactamente eso.
    """
    _, _, fila = _declaro_y_entreno(
        db, cfg, declara="bici", ejecuta="dia_2", pesos_de=LUNES - timedelta(days=9)
    )
    assert fila.motivo_suelto.startswith("tocaba Día 1 y entrenaste Día 2"), (
        fila.motivo_suelto
    )


def test_una_rutina_sin_pesos_escritos_nunca_no_se_inventa_una_fecha(db, cfg):
    """El estado normal de una rutina que todavía no ha salido en ninguna vuelta.

    No es un fallo y no se disimula con la fecha de hoy ni con un hueco: se
    nombra. Una fecha inventada aquí sería peor que el silencio, porque es
    exactamente el dato que se va a usar para entender por qué la sesión fue
    como fue.
    """
    _, _, fila = _declaro_y_entreno(db, cfg, declara="dia_2", ejecuta="dia_1")

    assert "sin pesos escritos nunca por el sistema" in fila.motivo_suelto, (
        fila.motivo_suelto
    )
    assert "los pesos del" not in fila.motivo_suelto, fila.motivo_suelto


@pytest.mark.parametrize("estado", ["dry_run", "read_only", "skipped", "error", "reverted"])
def test_una_escritura_que_no_toco_hevy_no_fecha_ningun_peso(db, cfg, estado):
    """Solo `ok` cuenta, y la lista de lo que no cuenta importa tanto como esa.

    `dry_run` no tocó nada, `read_only` se paró antes del PUT, `skipped` ni lo
    intentó y `error` pudo llegar a medias. `reverted` es el que más engaña: es
    una escritura de verdad, pero su contenido es la rutina ANTERIOR, así que
    fecharía los pesos el día en que se deshizo un cambio en vez del día en que
    se pusieron.

    Cualquiera de los cinco dado por bueno produce una frase concreta y falsa
    -«con los pesos del 03/09»- sobre unos pesos que ese día no se escribieron.
    """
    _, _, fila = _declaro_y_entreno(
        db, cfg, declara="dia_2", ejecuta="dia_1",
        pesos_de=LUNES - timedelta(days=12), estado_pesos=estado,
    )
    assert "sin pesos escritos nunca por el sistema" in fila.motivo_suelto, (
        fila.motivo_suelto
    )


def test_una_rutina_de_fuera_del_ciclo_conserva_el_motivo_de_siempre(db, cfg):
    """El HIIT no entra por aquí, y no es un detalle de reparto.

    La frase nueva habla de «los ajustes de la mañana fueron a la otra», que
    presupone que las dos rutinas son alternativas: una en vez de la otra. Un
    HIIT no es una alternativa a la fuerza, es un añadido, y contarlo con esas
    palabras diría que ese día elegí HIIT en lugar de entrenar.
    """
    upsert_checkin(db, LUNES, {"chosen_session": "dia_2"}, config=cfg)
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())

    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="hiit_suelto")
    w["routine_id"] = _rid(cfg, "hiit_dia_1")
    run_reconcile(db, cfg, LUNES, workouts=[w])

    fila = db.scalars(
        select(WorkoutLog).where(WorkoutLog.hevy_workout_id == "hiit_suelto")
    ).one()
    assert "HIIT por libre" in fila.motivo_suelto, fila.motivo_suelto
    assert "entrenaste" not in fila.motivo_suelto, fila.motivo_suelto


def test_una_rutina_del_config_que_no_esta_en_la_rotacion_tampoco_entra(db, cfg_copia):
    """Ni del ciclo ni HIIT: una rutina suelta del `config.yaml`.

    Hoy no existe ninguna, y por eso este test monta una. El HIIT se desvía
    antes por su propia rama, así que sin este caso la condición `rk in ciclo`
    no la comprueba nadie: da igual escribirla que poner `rk is not None`, y la
    suite entera sigue verde.

    Pero el día que se añada una rutina de movilidad, o de core, o lo que sea,
    hacerla un martes tiene que contarse como lo que es -trabajo extra que el
    plan no pedía- y no como «declaraste Día 1 y entrenaste Movilidad, con los
    pesos del 03/09», que sugiere que era una alternativa a la sesión y que
    alguien se equivocó de rutina.
    """
    cfg_copia.raw["routines"]["movilidad"] = {
        "title": "Movilidad",
        "hevy_routine_id": "rid-movilidad",
        "exercises": [],
    }
    assert "movilidad" not in cfg_copia.raw["rotation"]["order"]

    corre(db, cfg_copia, hevy=HevyFalso(), tg=TelegramFalso())
    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="movilidad")
    w["routine_id"] = "rid-movilidad"
    run_reconcile(db, cfg_copia, LUNES, workouts=[w])

    fila = db.scalars(
        select(WorkoutLog).where(WorkoutLog.hevy_workout_id == "movilidad")
    ).one()
    assert fila.motivo_suelto == "es movilidad y en la rotación tocaba dia_1", (
        fila.motivo_suelto
    )


def test_la_fecha_de_los_pesos_es_la_ultima_escritura_y_no_la_primera(db, cfg):
    """Con dos mañanas en que se escribió esa rutina, vale la de después.

    Lo que hay AHORA en Hevy lo puso la última, y es de lo que hay ahora de lo
    que habla la frase. Ordenado al revés, el motivo fecharía los pesos meses
    atrás y haría parecer abandonada una rutina que se hizo hace dos semanas.
    """
    _escrita_el(db, "dia_1", LUNES - timedelta(days=40))
    _, _, fila = _declaro_y_entreno(
        db, cfg, declara="dia_2", ejecuta="dia_1", pesos_de=LUNES - timedelta(days=12)
    )

    esperada = (LUNES - timedelta(days=12)).strftime("%d/%m")
    assert f"con los pesos del {esperada}" in fila.motivo_suelto, fila.motivo_suelto


def test_reconciliar_una_noche_vieja_no_fecha_los_pesos_en_el_futuro(db, cfg):
    """Este job se reintenta y se puede lanzar a mano meses después.

    Rehecha la noche del lunes desde el sábado siguiente, la base ya tiene la
    escritura del miércoles, que el lunes por la noche no existía. Sin el tope
    en `hasta`, la frase diría que esa mañana entrené con unos pesos que se
    escribieron dos días más tarde: no es un desajuste de un día, es una
    afirmación sobre el pasado que se contradice sola.
    """
    _, _, fila = _declaro_y_entreno(
        db, cfg, declara="dia_2", ejecuta="dia_1", pesos_de=LUNES - timedelta(days=12)
    )
    del fila  # la primera pasada solo deja el escenario montado

    # Llega el miércoles, el Día 1 vuelve a tocar y se escribe. Y el sábado se
    # relanza a mano la reconciliación del lunes.
    _escrita_el(db, "dia_1", LUNES + timedelta(days=2))
    db.query(WorkoutLog).delete()
    db.flush()

    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="otra_vez")
    w["routine_id"] = _rid(cfg, "dia_1")
    run_reconcile(db, cfg, LUNES, workouts=[w])

    rehecha = db.scalars(
        select(WorkoutLog).where(WorkoutLog.hevy_workout_id == "otra_vez")
    ).one()
    esperada = (LUNES - timedelta(days=12)).strftime("%d/%m")
    assert f"con los pesos del {esperada}" in rehecha.motivo_suelto, (
        rehecha.motivo_suelto
    )


def test_hacer_la_rutina_declarada_no_deja_ningun_motivo(db, cfg):
    """El caso de todos los días: se declara una y se hace ésa.

    Es el que impide que el bloque nuevo se convierta en un aviso permanente.
    Escrita la comprobación sobre `sesion_elegida` en vez de sobre la rutina del
    plan, un día perfectamente normal -declaro Día 2, hago Día 2- saldría cada
    noche como «visto en Hevy, fuera del plan».
    """
    upsert_checkin(db, LUNES, {"chosen_session": "dia_2"}, config=cfg)
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    assert plan["routine"] == "dia_2"

    w = _entrenamiento_completo(plan, wid="lo_declarado")
    w["routine_id"] = _rid(cfg, "dia_2")
    res = run_reconcile(db, cfg, LUNES, workouts=[w])

    assert res.sueltos == [], res.sueltos
    fila = db.scalars(select(WorkoutLog)).one()
    assert fila.unplanned is False
    assert fila.motivo_suelto is None


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

    Es la frase del usuario, literal: "un wall ball que no hice no debe frenar
    la progresión de la prensa". Cuando el bloque HIIT se AÑADÍA a los
    ejercicios de la rutina, `executed` llevaba claves de HIIT y el veredicto
    del día se volvía False en cuanto faltara una de ellas; esas claves contaban
    para la progresión de fuerza, así que saltarse el HIIT congelaba la carga de
    la sesión de fuerza para siempre.

    Aquí se hace la fuerza ENTERA y nada del HIIT. La racha de fuerza tiene que
    avanzar exactamente igual, y la del HIIT tiene que romperse: las dos cosas,
    porque "no se contamina" y "no se mide" se parecen mucho vistas desde el
    lado de la fuerza y solo una de las dos es la que se quiere.
    """
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    assert plan["hiit_block"] == "hiit_dia_1", "el escenario ya no lleva HIIT"

    solo_fuerza = _ejecuta(plan, wid="fuerza", rid=_rid(cfg_lunes, "dia_1"))

    res = run_reconcile(db, cfg_lunes, LUNES, workouts=[solo_fuerza])
    assert res.avanzado

    estado = load_state(db, program_start=cfg_lunes.program_start)
    base = [e["key"] for e in cfg_lunes.raw["routines"]["dia_1"]["exercises"]]
    assert all(estado.clean_sessions.get(("dia_1", k), 0) == 1 for k in base), (
        "saltarse el HIIT ha roto la racha de la fuerza: "
        f"{[(k, estado.clean_sessions.get(('dia_1', k), 0)) for k in base]}"
    )
    bloque = [e["key"] for e in cfg_lunes.raw["routines"]["hiit_dia_1"]["exercises"]]
    assert all(estado.clean_sessions.get(("hiit_dia_1", k), 0) == 0 for k in bloque), (
        "el HIIT no se hizo y su racha ha avanzado igual: no se está midiendo, "
        f"{[(k, estado.clean_sessions.get(('hiit_dia_1', k), 0)) for k in bloque]}"
    )
    assert all(estado.compliance.get(("hiit_dia_1", k)) is False for k in bloque), (
        "el cumplimiento del HIIT no se ha apuntado bajo su propia clave: "
        f"{ {k: estado.compliance.get(('hiit_dia_1', k)) for k in bloque} }"
    )


def _titulos(payload: dict) -> list[str]:
    return [e["title"] for e in payload["routine"]["exercises"]]


def test_el_hiit_se_escribe_en_su_rutina_de_hevy_y_no_dentro_de_la_fuerza(
    db, cfg_lunes
):
    """Dos entrenamientos distintos son dos rutinas distintas en Hevy.

    Mientras el bloque viajaba dentro de `session.exercises`, el PUT de la
    mañana metía los wall balls al final de `Día 1` y la rutina `Día 1 HIIT` de
    la cuenta no se tocaba nunca. Eso obligaba a registrarlo todo de una sentada
    para que el sistema lo leyera, y era justo lo que hacía que un wall ball sin
    hacer contaminara la racha de la prensa.
    """
    hevy, tg = HevyFalso(), TelegramFalso()
    res = corre(db, cfg_lunes, hevy=hevy, tg=tg)
    assert res.decision.session.hiit_block == "hiit_dia_1", (
        "el escenario ya no lleva HIIT; el test hay que rehacerlo"
    )

    assert res.hevy_status == "ok", f"{res.hevy_status} ({res.hevy_reason})"
    assert res.hevy_hiit_status == "ok", (
        f"{res.hevy_hiit_status} ({res.hevy_hiit_reason})"
    )

    escrito = dict(hevy.llamadas)
    assert set(escrito) == {_rid(cfg_lunes, "dia_1"), _rid(cfg_lunes, "hiit_dia_1")}, (
        f"no se han tocado las dos rutinas: {list(escrito)}"
    )

    del_bloque = {
        e["name"] for e in cfg_lunes.raw["routines"]["hiit_dia_1"]["exercises"]
    }
    en_fuerza = set(_titulos(escrito[_rid(cfg_lunes, "dia_1")]))
    en_hiit = set(_titulos(escrito[_rid(cfg_lunes, "hiit_dia_1")]))
    assert del_bloque <= en_hiit, f"falta bloque en su rutina: {del_bloque - en_hiit}"
    assert not (del_bloque & en_fuerza), (
        f"el HIIT sigue metido en la rutina de fuerza: {del_bloque & en_fuerza}"
    )


def test_el_estado_de_las_dos_escrituras_va_separado(db, cfg_lunes):
    """Dos filas en `hevy_writes`, cada una con SU rutina y SU estado.

    Con un solo `hevy_status` en el resultado, la escritura del bloque quedaba
    tapada por la de la fuerza: si el PUT del HIIT fallaba y el de la fuerza
    salía bien, el día se leía como «ok» y en la cuenta quedaba un bloque viejo
    sin que nada lo dijera.
    """
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())

    filas = db.scalars(select(HevyWrite).order_by(HevyWrite.id)).all()
    por_rutina = {f.routine_key: f for f in filas}
    assert set(por_rutina) == {"dia_1", "hiit_dia_1"}, [f.routine_key for f in filas]
    assert por_rutina["hiit_dia_1"].status == "ok"
    assert por_rutina["hiit_dia_1"].hevy_routine_id == _rid(cfg_lunes, "hiit_dia_1")
    assert por_rutina["hiit_dia_1"].reason, "una escritura sin motivo es media auditoría"


def test_un_checkin_rojo_tardio_tambien_deshace_el_hiit(db, cfg_lunes):
    """La reversión tiene que cubrir las DOS rutinas que se escribieron.

    Es el mismo agujero que ya estaba cerrado para la fuerza, reabierto por la
    separación: a las 09:00 sin formulario sale verde y se escriben `Día 1` y
    `Día 1 HIIT`; a las 10:30 llega un check-in rojo y la sesión pasa a ser
    recuperación. Deshacer solo la fuerza dejaría en la app un bloque de HIIT
    que la decisión vigente no pide, en un día rojo y con una hernia L4-L5.
    """
    hevy, tg = HevyFalso(), TelegramFalso()
    manana_sin_checkin(db, cfg_lunes, hevy, tg)
    assert _plan_guardado(db)["hiit_block"] == "hiit_dia_1", "el montaje no lleva HIIT"

    res = checkin_tardio(db, cfg_lunes, hevy, tg, CHECKIN_ROJO)

    assert res.decision.light == "red"
    assert res.hevy_status == "reverted"
    assert res.hevy_hiit_status == "reverted", (
        f"el bloque se ha quedado escrito: {res.hevy_hiit_status} "
        f"({res.hevy_hiit_reason})"
    )
    assert sorted(r for r, _ in hevy.reversiones) == sorted(
        [_rid(cfg_lunes, "dia_1"), _rid(cfg_lunes, "hiit_dia_1")]
    ), f"no se han deshecho las dos: {hevy.reversiones}"

    vueltas = [
        f for f in db.scalars(select(HevyWrite)).all() if f.status == "reverted"
    ]
    assert sorted(f.routine_key for f in vueltas) == ["dia_1", "hiit_dia_1"]


def test_en_el_bloque_hiit_la_ultima_serie_corta_tampoco_borra_la_racha(db, cfg_lunes):
    """El bloque tiene su propio `apply_execution` y su propio cumplimiento: sin
    esto, la regla del 12/12/9 valdría en la fuerza y no en el HIIT sin que
    nada lo dijera."""
    from app import repository as repo

    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    bloque = plan["hiit"]
    subidos = set(repo.progressed_keys(repo.current_decision(db, LUNES), hiit=True))
    ex = next(
        e for e in bloque["exercises"]
        if e["key"] not in subidos and any(s.get("reps") for s in e["sets"])
    )
    clave = ("hiit_dia_1", ex["key"])
    estado = load_state(db, program_start=cfg_lunes.program_start)
    estado.clean_sessions[clave] = 1
    save_state(db, estado, day=LUNES)

    hecho = copy.deepcopy(bloque)
    for e in hecho["exercises"]:
        if e["key"] == ex["key"]:
            ultima = [s for s in e["sets"] if s.get("type") != "warmup"][-1]
            ultima["reps"] = ultima["reps"] - 3
    w = _ejecuta(hecho, wid="hiit", rid=_rid(cfg_lunes, "hiit_dia_1"))

    run_reconcile(db, cfg_lunes, LUNES, workouts=[w])

    estado = load_state(db, program_start=cfg_lunes.program_start)
    assert estado.clean_sessions[clave] == 1, "en el HIIT la última serie corta borra la racha"
    assert estado.compliance[clave] is False


def test_la_carga_del_hiit_se_adopta_bajo_la_clave_del_bloque(db, cfg_lunes):
    """El wall ball sube en `hiit_dia_1`, que es donde el config lo declara.

    Antes la adopción de la carga ejecutada del bloque se guardaba bajo `dia_1`,
    la rutina de fuerza de ese día. Nadie lee esa fila -`dia_1` no tiene esa
    clave- y además el bloque rota, así que la misma plancha acababa repartida
    entre `dia_1` y `dia_2`. Resultado: ninguna carga del HIIT llegó nunca a
    moverse, y no había ningún error por ninguna parte.
    """
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    bloque = plan["hiit"]
    ex = next(e for e in bloque["exercises"] if e["key"] == "wall_ball")
    antes = float(ex["sets"][0]["weight_kg"])

    hecho = copy.deepcopy(bloque)
    for e in hecho["exercises"]:
        if e["key"] == "wall_ball":
            for s in e["sets"]:
                s["weight_kg"] = antes + 1.0
    w = _ejecuta(hecho, wid="hiit", rid=_rid(cfg_lunes, "hiit_dia_1"))

    res = run_reconcile(db, cfg_lunes, LUNES, workouts=[w])

    adoptada = [
        a for a in res.adopciones if a["key"] == "wall_ball" and a["applied"]
    ]
    assert adoptada, f"el peso del wall ball no se ha adoptado: {res.adopciones}"
    assert adoptada[0]["routine"] == "hiit_dia_1", (
        f"adoptado bajo {adoptada[0]['routine']!r}, que no es donde vive"
    )

    estado = load_state(db, program_start=cfg_lunes.program_start)
    series = estado.current_sets.get(("hiit_dia_1", "wall_ball"))
    assert series, (
        "la carga no ha quedado bajo la clave del bloque: "
        f"{[k for k in estado.current_sets if k[1] == 'wall_ball']}"
    )
    assert float(series[0]["weight_kg"]) > antes
    assert ("dia_1", "wall_ball") not in estado.current_sets, (
        "la carga del HIIT sigue ensuciando la rutina de fuerza"
    )


# ---------------------------------------------------------------------------
# La progresión del bloque HIIT, que nunca había corrido
# ---------------------------------------------------------------------------
#
# `build_session` planificaba la progresión de la rutina de FUERZA en su paso 2 y
# montaba el bloque HIIT en el paso 6, con el plan de la fuerza ya gastado. El
# bloque se construía siempre con `con_carga_vigente` y nada más: la plancha
# frontal salía a 30 s el primer día y a 30 s el día doscientos, con
# `progression_type: volume` y `max_seconds: 60` declarados en el YAML desde el
# principio y sin un solo error por ninguna parte.
#
# Lo que se vigila aquí son las cuatro costuras del arreglo, no que "suba":
#
#   - que suba de verdad, en el plan que se guarda y se escribe en Hevy;
#   - que la subida se PERSISTA bajo su propia clave, porque una subida que la
#     noche no ve es una racha que no se gasta y una plancha que sube cinco
#     segundos CADA sesión hasta el techo sin que nada falle;
#   - que el plan del bloque se anule cuando el bloque no llega a entrar, para
#     que el registro no guarde una subida que nadie ejecutó;
#   - que el techo se anuncie, que es el único aviso que convierte "parado" en
#     accionable.


def _estado_con_racha_de_hiit(cfg) -> EngineState:
    """El bloque HIIT estrenado y con una sesión limpia en todos sus ejercicios.

    Es el estado normal tras UNA noche de haber hecho el bloque entero, y es el
    mínimo que abre la puerta: `default_clean_sessions_required` es 1. Se monta
    a mano en vez de encadenar dos días reales porque la rotación movería el
    puntero a `dia_2` y el bloque del segundo día no lleva plancha; el test
    acabaría comprobando la ausencia del ejercicio en vez de su progresión.

    `compliance` va con las claves puestas a propósito y no vacío: es lo que
    hace que `estrenada("hiit_dia_1")` sea cierto, y sin ello la puerta se
    cierra con el motivo del estreno -que no es un fallo, pero tampoco es este
    escenario-.
    """
    claves = [e["key"] for e in cfg.raw["routines"]["hiit_dia_1"]["exercises"]]
    return EngineState(
        compliance={("hiit_dia_1", k): True for k in claves},
        clean_sessions={("hiit_dia_1", k): 1 for k in claves},
        program_start=cfg.program_start,
    )


def _monta_la_racha(db, cfg, estado: EngineState | None = None) -> None:
    """El estado de ayer Y el check-in de hoy, que hacen falta los dos.

    El check-in no es decorado: el freno `lumbar_bloquea_todo` mira
    `lower_discomfort`, y sin ese dato la puerta de la progresión se cierra por
    indeterminación -"sin ese dato no se sube carga"- antes de llegar a mirar
    ninguna racha. Un test de progresión sin formulario no comprueba la
    progresión: comprueba el freno.
    """
    save_state(db, estado or _estado_con_racha_de_hiit(cfg), day=LUNES - timedelta(days=2))
    upsert_checkin(db, LUNES, dict(CHECKIN_TRANQUILO), config=cfg)
    db.flush()


def _plancha(bloque: dict) -> dict:
    return next(e for e in bloque["exercises"] if e["key"] == "plancha_frontal")


def _segundos(bloque: dict) -> list[int]:
    return [int(s["duration_s"]) for s in _plancha(bloque)["sets"]]


def test_la_plancha_del_hiit_sube_de_treinta_a_treinta_y_cinco(db, cfg_lunes):
    """La progresión que llevaba parada desde el primer día.

    El bloque se planifica con SU propio `plan_progression` -no con el de la
    fuerza-, porque los dos tienen estado, semáforo de última sesión y cupos de
    `volume_safety` distintos. Aplicar aquí el plan de `dia_1` sería calcular
    contra el estado de una rutina y prescribir sobre otra: números plausibles y
    mal.
    """
    _monta_la_racha(db, cfg_lunes)

    res = corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())
    assert res.decision.session.hiit_block == "hiit_dia_1", "el montaje no lleva HIIT"

    bloque = _plan_guardado(db)["hiit"]
    assert _segundos(bloque) == [35, 35, 35], (
        f"la plancha sigue clavada donde la dejó el YAML: {_segundos(bloque)}"
    )
    assert res.decision.progression_hiit is not None, (
        "el bloque entró y no se guardó plan de progresión propio"
    )
    assert [c.name for c in res.decision.progression_hiit.changes] == ["Plancha frontal"]
    assert any("30→35 s" in c.text() for c in res.decision.progression_hiit.changes)


def test_la_carga_vigente_del_bloque_se_fija_DESPUES_de_la_subida(db, cfg_lunes):
    """El objetivo vigente es lo que se acaba de prescribir, no lo de ayer.

    `target_sets` se calcula sobre los ejercicios YA progresados, igual que el
    paso 2b de la fuerza, y de ahí sale la carga vigente que guarda el estado.
    Calculado antes, la plancha subiría a 35 s en Hevy y la base seguiría
    diciendo 30: mañana `con_carga_vigente` la sacaría otra vez a 30 y la subida
    volvería a ser "la primera" cada día, con la app y la base contando cosas
    distintas y ninguna de las dos quejándose.
    """
    _monta_la_racha(db, cfg_lunes)
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())

    vigente = load_state(db, program_start=cfg_lunes.program_start).current_sets
    series = vigente.get(("hiit_dia_1", "plancha_frontal"))
    assert series and [int(s["duration_s"]) for s in series] == [35, 35, 35], series


def test_la_subida_del_hiit_gasta_su_racha_esa_misma_noche(db, cfg_lunes):
    """EL SEGURO CONTRA LA PLANCHA QUE SUBE CINCO SEGUNDOS CADA DÍA.

    La subida se apunta en `progression_json`, y de ahí la saca la noche para
    poner a cero la racha del ejercicio que ha subido: las sesiones limpias que
    pagaron la subida ya se han gastado en ella. El plan del bloque vive bajo la
    clave `"hiit"` de ese JSON, y `progressed_keys(fila, hiit=True)` es quien lo
    lee.

    Guardar solo el plan de la fuerza -que es lo que se hacía- no da ningún
    error: la racha de la plancha se queda intacta, al día siguiente vuelve a
    cumplir el requisito, y la plancha sube otros cinco segundos. De 30 a 60 en
    seis sesiones, sin una sola sesión limpia que lo pague y sin nada que falle.

    Se comprueban las DOS mitades. Que la de la plancha se ponga a cero, y que
    la de sus compañeras avance a 2: "no se gasta" y "se ha roto la racha
    entera" se ven igual mirando solo la plancha, y solo una de las dos es lo
    que se quiere.
    """
    _monta_la_racha(db, cfg_lunes)
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())

    plan = _plan_guardado(db)
    assert _segundos(plan["hiit"]) == [35, 35, 35], "el montaje no ha subido nada"

    fuerza = _ejecuta(plan, wid="fuerza", rid=_rid(cfg_lunes, "dia_1"))
    hiit = _ejecuta(plan["hiit"], wid="hiit", rid=_rid(cfg_lunes, "hiit_dia_1"))
    run_reconcile(db, cfg_lunes, LUNES, workouts=[fuerza, hiit])

    estado = load_state(db, program_start=cfg_lunes.program_start)
    assert estado.clean_sessions[("hiit_dia_1", "plancha_frontal")] == 0, (
        "la plancha ha subido hoy y conserva la racha: mañana sube otra vez "
        "sin haberla pagado"
    )
    # La otra mitad -que se gaste SOLO la del que subió- necesita que haya
    # otros con los que comparar. Si el bloque se quedara con un ejercicio,
    # `otras` sale vacía, `all([])` es cierto y la mitad que distingue "gastar
    # la racha del que subió" de "romper la racha entera" deja de existir.
    otras = [
        e["key"]
        for e in cfg_lunes.raw["routines"]["hiit_dia_1"]["exercises"]
        if e["key"] != "plancha_frontal"
    ]
    assert otras, (
        "el bloque HIIT solo tiene la plancha: no hay con qué contrastar que la "
        "racha se gasta solo en el ejercicio que subió"
    )
    assert all(estado.clean_sessions[("hiit_dia_1", k)] == 2 for k in otras), (
        "se ha roto la racha de todo el bloque, no solo la del que subió: "
        f"{ {k: estado.clean_sessions.get(('hiit_dia_1', k)) for k in otras} }"
    )


def test_las_claves_progresadas_del_bloque_no_se_sacan_de_la_lista_de_la_fuerza(
    db, cfg_lunes
):
    """Por qué `progressed_keys` necesita el `hiit=True` y no basta con filtrar.

    Antes la noche cogía las claves progresadas de la FUERZA y se quedaba con
    las que pertenecían al bloque. Eso acierta solo mientras las dos rutinas no
    compartan ninguna clave: el día que `dia_1` y `hiit_dia_1` tengan un
    ejercicio con el mismo nombre -una plancha, por ejemplo- una subida de la
    fuerza gastaría la racha del bloque, o al revés.

    Aquí las dos listas tienen que salir distintas y cada una de su plan. Si
    alguien vuelve a filtrar una sola lista, la del bloque acabará siendo un
    subconjunto de la de la fuerza y este test lo dice.
    """
    from app.repository import current_decision, progressed_keys

    _monta_la_racha(db, cfg_lunes)
    corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())

    fila = current_decision(db, LUNES)
    del_bloque = progressed_keys(fila, hiit=True)
    assert del_bloque == ["plancha_frontal"], del_bloque
    assert "plancha_frontal" not in progressed_keys(fila), (
        "la clave del bloque está saliendo por la lista de la fuerza"
    )


def test_si_el_bloque_no_entra_no_queda_plan_de_progresion_guardado(db, cfg):
    """Un plan que nadie ejecutó no se guarda como si se hubiera prescrito.

    El plan del bloque se calcula ANTES de saber si el bloque entra -hace falta
    para construirlo-, y hay cuatro salidas por las que puede no entrar: la
    semana no le toca, el día es rojo, no hay fuerza hoy o la rutina no declara
    bloque. Si el plan sobreviviera a esas salidas, el registro diría que la
    plancha subió a 35 s un día en que la plancha no se planificó, y esa subida
    fantasma se leería como prescripción a la hora de auditar por qué el
    ejercicio está donde está.

    El escenario es el config real con su fecha de arranque: `hiit_applies` no
    da el bloque, pero `hiit.blocks` sí declara cuál le tocaría a `dia_1`, así
    que el plan SÍ se calcula y esta es la única guardia que lo borra.
    """
    _monta_la_racha(db, cfg)

    res = corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    assert res.decision.session.hiit is None, "el montaje necesita un día SIN bloque"

    assert res.decision.progression_hiit is None, (
        "hay plan de progresión de un bloque que no se llegó a prescribir"
    )
    fila = db.scalars(select(Decision).where(Decision.date == LUNES)).one()
    assert "hiit" not in json.loads(fila.progression_json or "{}"), (
        f"la subida fantasma ha quedado guardada: {fila.progression_json}"
    )


def test_la_plancha_en_su_techo_se_anuncia_en_el_mensaje(db, cfg_lunes):
    """60 s y se acabó: el primer ejercicio del programa que toca techo.

    El YAML la saca a 30 s y el tope declarado son 60, así que la plancha va a
    llegar antes que ningún otro. Un techo no dicho deja el ejercicio clavado
    para siempre con toda la pinta de que el sistema lo ha olvidado; dicho el
    día que ocurre, "toca cambiar el ejercicio" es accionable.

    El aviso se comprueba en el TEXTO que se manda y no en `plan.ceilings`,
    porque el fallo que esto cubre no es que el techo no se detecte -se detectó
    siempre- sino que el mensaje solo leía el plan de la fuerza: el bloque podía
    estar en su techo y `render_telegram` no tenía de dónde sacarlo.
    """
    estado = _estado_con_racha_de_hiit(cfg_lunes)
    estado.current_sets[("hiit_dia_1", "plancha_frontal")] = [
        {"duration_s": 60} for _ in range(3)
    ]
    _monta_la_racha(db, cfg_lunes, estado)

    tg = TelegramFalso()
    res = corre(db, cfg_lunes, hevy=HevyFalso(), tg=tg)
    assert res.decision.session.hiit_block == "hiit_dia_1", "el montaje no lleva HIIT"

    assert _segundos(_plan_guardado(db)["hiit"]) == [60, 60, 60], "se ha pasado del tope"
    assert res.decision.progression_hiit.ceilings == ["Plancha frontal"], (
        res.decision.progression_hiit.ceilings
    )
    assert "Plancha frontal: techo alcanzado" in tg.enviados[0], tg.enviados[0]


def test_el_bloque_obedece_a_allow_progression_y_no_al_hecho_de_estar_puesto(
    db, cfg_lunes
):
    """`permitida` se lee del config; no se da por hecho porque hoy coincida.

    HOY ESTE CASO NO PUEDE OCURRIR, Y POR ESO ESTÁ ESCRITO ASÍ. El bloque solo
    entra en verde (`hiit.only_on_green`) y en verde `allow_progression` está a
    `true`, de modo que "el bloque está puesto" y "hoy se puede progresar" son
    la misma cosa. Atar una a la otra en el código no rompería nada esta semana:
    rompería el día que el ámbar deje entrar el HIIT recortado, y entonces el
    volumen del bloque subiría mientras la fuerza está recortada, que es
    exactamente al revés de lo que el ámbar significa.

    Así que el escenario se fabrica moviendo el interruptor que gobierna la
    fuerza -`actions.green.allow_progression`- y comprobando que el bloque se
    entera. Si alguien sustituye `permitida` por un `True`, este test es lo
    único que lo dice.

    Y la nota importa tanto como la no-subida: sin ella, un día en que la
    progresión está deshabilitada y un día en que el cable se ha vuelto a
    desconectar se ven idénticos desde fuera -la plancha en 30 s las dos veces-,
    que es precisamente cómo esto pasó inadvertido desde el primer día.
    """
    cfg_lunes.raw["actions"]["green"]["allow_progression"] = False
    _monta_la_racha(db, cfg_lunes)

    res = corre(db, cfg_lunes, hevy=HevyFalso(), tg=TelegramFalso())
    assert res.decision.light == "green"
    assert res.decision.session.hiit is not None, "el montaje necesita el bloque puesto"

    bloque = _plan_guardado(db)["hiit"]
    assert _segundos(bloque) == [30, 30, 30], (
        f"la plancha ha subido en un día que no lo permitía: {_segundos(bloque)}"
    )
    assert any("el semáforo no la permite" in n for n in bloque["notes"]), (
        f"no ha subido y no se dice por qué: {bloque['notes']}"
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


def test_la_ultima_serie_corta_no_borra_la_racha_de_punta_a_punta(db, cfg):
    """De Hevy a la tabla: el 12/12/9 deja la racha donde estaba.

    Y cierra la puerta de mañana: «no cuenta para el próximo día, que seguiría
    siendo 12/12/12». Un ejercicio que la mañana haya subido se salta, porque
    su racha vuelve a cero por la subida y el test no probaría nada.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    from app import repository as repo

    subidos = set(repo.progressed_keys(repo.current_decision(db, LUNES)))
    ex = next(
        e for e in plan["exercises"]
        if e["key"] not in subidos
        and any((s.get("weight_kg") or 0) > 0 for s in e.get("sets") or [])
        and any(s.get("reps") for s in e.get("sets") or [])
    )
    clave = ("dia_1", ex["key"])
    estado = load_state(db, program_start=cfg.program_start)
    estado.clean_sessions[clave] = 1
    save_state(db, estado, day=LUNES)

    w = _entrenamiento_completo(plan)
    for e in w["exercises"]:
        if e["exercise_template_id"] == ex.get("template_id"):
            ultima = [s for s in e["sets"] if s.get("type") != "warmup"][-1]
            ultima["reps"] = ultima["reps"] - 3

    run_reconcile(db, cfg, LUNES, workouts=[w])

    estado = load_state(db, program_start=cfg.program_start)
    assert estado.clean_sessions[clave] == 1, "la última serie corta ha tocado la racha"
    assert estado.compliance[clave] is False, "cuenta para mañana, y no debería"


def test_una_rampa_hecha_se_guarda_con_su_forma_y_no_desplazada(db, cfg):
    """El cable entero del 25/09/2026: de Hevy a la tabla, serie a serie.

    La primera serie más ligera de lo pedido y la última más pesada. Con el
    desplazamiento, el objetivo guardado subía TODAS las series lo que subió
    la última, y la primera quedaba por encima de lo que se había hecho.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    plan = _plan_guardado(db)
    ex = _primer_ejercicio_con_peso(plan)

    w = _entrenamiento_completo(plan)
    hechas = []
    for e in w["exercises"]:
        if e["exercise_template_id"] == ex.get("template_id"):
            efectivas = [s for s in e["sets"] if s.get("type") != "warmup"]
            efectivas[0]["weight_kg"] = round(efectivas[0]["weight_kg"] - 5, 2)
            efectivas[-1]["weight_kg"] = round(efectivas[-1]["weight_kg"] + 2.5, 2)
            hechas = [s["weight_kg"] for s in efectivas]
    assert len(hechas) >= 2, "hace falta un ejercicio con al menos dos series"

    run_reconcile(db, cfg, LUNES, workouts=[w])

    estado = load_state(db, program_start=cfg.program_start)
    guardadas = [s["weight_kg"] for s in estado.current_sets[("dia_1", ex["key"])]]
    assert guardadas == hechas, (
        f"se hizo {hechas} y ha quedado {guardadas}: el objetivo no es lo que "
        f"se levantó"
    )


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
    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="hiit_suelto")
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
    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="suelto")
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
    w = _entrenamiento_completo({"exercises": []}, sin_plan=True, wid="suelto")
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


@no_es_doble("el resultado de la simulacion de este modulo")
@dataclass
class _Simulacion:
    """Lo que pasó en las seis semanas, no solo lo que se quería mirar.

    Existe porque la versión anterior devolvía únicamente el `Counter` de tipos
    de progresión, y eso dejaba sin vigilar la premisa del propio test: que los
    42 días sean 42 días distintos. No lo eran. Ver `_entrenamiento_completo`.
    """

    tipos: Counter
    rutinas: Counter          # qué rutina se planificó cada día
    filas: int                # entrenamientos registrados
    sueltos: int              # cuántos se anotaron como fuera del plan
    sin_veredicto: int        # cuántos quedaron sin cumplimiento evaluado


def _seis_semanas(db, cfg, *, reconciliar: bool) -> _Simulacion:
    """Seis semanas de días verdes. Devuelve qué pasó en ellas."""
    from app.repository import upsert_checkin

    tipos: Counter = Counter()
    rutinas: Counter = Counter()
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
        plan = _plan_guardado(db, d)
        if plan.get("exercises"):
            rutinas[str(plan.get("routine"))] += 1
            if reconciliar:
                run_reconcile(
                    db, cfg, d,
                    workouts=[_entrenamiento_completo(plan, wid=f"w{i}", day=d)],
                )
    # EL `if` DE ARRIBA ES UNA PUERTA QUE PUEDE CERRARSE SOLA.
    # Los 42 días de esta simulación son verdes por construcción -el check-in va
    # tranquilo y las métricas son planas-, así que los 42 planifican fuerza. Si
    # `exercises` dejara de llamarse así, el `if` sería falso las 42 veces: cero
    # reconciliaciones, cero subidas, y la simulación mediría seis semanas de no
    # hacer nada mientras su nombre sigue diciendo seis semanas de entrenar.
    assert sum(rutinas.values()) == 42, (
        f"{sum(rutinas.values())} de 42 días verdes han salido sin ejercicios; "
        "la simulación no está simulando lo que dice"
    )
    filas = db.scalars(select(WorkoutLog)).all()
    return _Simulacion(
        tipos=tipos,
        rutinas=rutinas,
        filas=len(filas),
        sueltos=sum(1 for f in filas if f.unplanned),
        sin_veredicto=sum(1 for f in filas if f.all_sets_at_target is None),
    )


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
    sim = _seis_semanas(db, cfg, reconciliar=False)
    assert sim.tipos.get("load", 0) == 0, (
        f"ha subido carga sin que nadie confirmara una sola sesión: {dict(sim.tipos)}"
    )
    # Aquí la rotación NO avanza, y está bien que no avance: sin un solo
    # entrenamiento registrado el motor no sabe que se haya hecho nada, así que
    # sigue proponiendo la misma rutina. Se afirma para que quede escrito que es
    # el comportamiento esperado de ESTE test y no el defecto que tenía el otro.
    assert set(sim.rutinas) == {"dia_1"}, (
        f"sin reconciliar nada la rotación ha avanzado sola: {dict(sim.rutinas)}"
    )
    assert sim.filas == 0


def test_reconciliando_el_programa_progresa(db, cfg):
    """La contraparte: con el bucle cerrado, seis semanas limpias suben peso.

    LAS TRES AFIRMACIONES DE ABAJO NO SON ADORNO, SON LA PREMISA
    ------------------------------------------------------------
    Este test estuvo en verde midiendo mucho menos de lo que su nombre dice.
    `_entrenamiento_completo` no mandaba `routine_id`, así que `routine_key`
    quedaba a NULL en las 42 filas, la rotación no avanzaba nunca y las «seis
    semanas» eran el `dia_1` cuarenta y dos veces, todas anotadas como hechas
    fuera del plan y sin cumplimiento evaluado. Con `load > 0` como única
    comprobación, el test pasaba igual: daba 7 subidas en vez de 15.

    O sea que lo que certificaba no era «el programa progresa», era «una rutina
    de las tres progresa algo». Ahora se afirma también la premisa: que los 42
    días sean 42 días del ciclo, que nada se registre como suelto y que todo
    tenga veredicto. Si el doble vuelve a perder el `routine_id`, esto se pone
    rojo en la línea que nombra la causa, no en la que mide la consecuencia.
    """
    sim = _seis_semanas(db, cfg, reconciliar=True)
    assert sim.tipos.get("load", 0) > 0, (
        f"seis semanas completando todo y la carga no ha subido nunca: {dict(sim.tipos)}"
    )
    assert sim.tipos.get("volume", 0) > 0

    assert set(sim.rutinas) == set(cfg.rotation_order()), (
        f"seis semanas no han recorrido el ciclo entero: {dict(sim.rutinas)}"
    )
    assert sim.sueltos == 0, (
        f"{sim.sueltos} de {sim.filas} sesiones hechas exactamente como pedía el "
        f"plan se han registrado como hechas por libre"
    )
    assert sim.sin_veredicto == 0, (
        f"{sim.sin_veredicto} de {sim.filas} sesiones se han quedado sin evaluar "
        f"el cumplimiento, que es justo lo que abre o cierra la puerta de carga"
    )


# ---------------------------------------------------------------------------
# apply_execution: la mitad que mueve las rachas
# ---------------------------------------------------------------------------


EJS = [{"key": "hip_thrust"}, {"key": "remo"}]


def test_la_racha_se_rompe_entera_no_se_decrementa():
    """"Sesiones limpias CONSECUTIVAS": decrementar sería una media."""
    st = EngineState(clean_sessions={("dia_1", "hip_thrust"): 5})
    apply_execution(
        st, routine_key="dia_1", exercises=EJS,
        executed={"hip_thrust": False, "remo": True}, mantener=(),
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
        executed={"hip_thrust": True, "remo": True}, mantener=(),
        progressed=["hip_thrust"],
    )
    assert st.clean_sessions[("dia_1", "hip_thrust")] == 0
    assert st.clean_sessions[("dia_1", "remo")] == 1


def test_la_ultima_serie_corta_ni_suma_ni_borra_la_racha():
    """«Es válida, pero no cuenta para el próximo día, que seguiría siendo
    12/12/12.» Decisión del usuario, 25/09/2026.

    No suma: `compliance` queda en False y eso cierra la puerta de mañana. No
    borra: la cadena posterior pide dos limpias seguidas, y empezar de cero cada
    vez que la última serie no sale entera la dejaba sin subir nunca.
    """
    st = EngineState(clean_sessions={("dia_1", "hip_thrust"): 1, ("dia_1", "remo"): 1})
    apply_execution(
        st, routine_key="dia_1", exercises=EJS,
        executed={"hip_thrust": False, "remo": False}, mantener={"hip_thrust"},
        progressed=(),
    )
    assert st.clean_sessions[("dia_1", "hip_thrust")] == 1, "se ha borrado o sumado"
    assert st.compliance[("dia_1", "hip_thrust")] is False, (
        "cuenta para mañana: la puerta de subir se abriría con la serie corta"
    )
    assert st.clean_sessions[("dia_1", "remo")] == 0, (
        "un fallo que no es el de la última serie sigue rompiendo la racha"
    )


def test_limpio_gana_a_mantener():
    """Si llega limpio, suma aunque también figure en `mantener`."""
    st = EngineState(clean_sessions={("dia_1", "hip_thrust"): 1})
    apply_execution(
        st, routine_key="dia_1", exercises=EJS,
        executed={"hip_thrust": True}, mantener={"hip_thrust"}, progressed=(),
    )
    assert st.clean_sessions[("dia_1", "hip_thrust")] == 2


def test_la_racha_es_por_rutina_y_ejercicio():
    """La plancha del Día 1 y la del Día 3 no comparten mérito."""
    st = EngineState(clean_sessions={("dia_3", "hip_thrust"): 4})
    apply_execution(
        st, routine_key="dia_1", exercises=EJS, executed={"hip_thrust": True},
        mantener=(), progressed=(),
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
    hoy, así que un verde hoy la corta por definición. Se fuerza con una HRV
    hundida UN solo día -53 sobre una base de 60, ratio 0,88- que es lo que
    dispara `hrv_baja_1d`. Un día solo y no dos, que dos serían `hrv_hundida_2d`
    y el día saldría rojo.
    """
    for i in range(45, 0, -1):
        luz = "amber" if i <= 8 else "green"
        db.add(Decision(date=LUNES - timedelta(days=i), light=luz,
                        trigger_rule="hrv_baja_1d" if luz == "amber" else None))
    db.commit()
    tg = TelegramFalso()
    res = run_daily(
        db, cfg, LUNES,
        metrics=[
            DayMetrics(date=LUNES, hrv=53.0, rhr=50.0, sleep_min=450,
                       sleep_score=80),
            *dias(LUNES - timedelta(days=1), 9, hrv=60.0, rhr=50.0,
                  sleep_min=450, sleep_score=80),
        ],
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


# ---------------------------------------------------------------------------
# Pensar el día sin ejecutarlo
# ---------------------------------------------------------------------------
#
# La mitad de arriba de `run_daily` no escribe nada: lee el check-in, fusiona el
# wellness, construye las señales y llama al motor. La de abajo guarda, escribe
# en Hevy y manda el Telegram. Esa frontera existía ya -se trazó para que una
# excepción pudiera decir hasta dónde había llegado la mañana- pero no tenía
# nombre propio ni forma de llamarse sola.
#
# Ahora sí, porque la previsualización la necesita: enseñar qué decidiría el
# sistema con unas respuestas que todavía no se han enviado. Y lo que hay que
# atar no es que devuelva la decisión -eso se vería enseguida- sino que NO
# ESCRIBA, que es lo que no se ve.


def test_pensar_el_dia_no_deja_ni_una_fila(db, cfg):
    """Lo que hace útil a la previsualización es exactamente lo que no hace.

    Si esto se rompe, el fallo es silencioso y de la peor clase: la pantalla
    dice «esto es lo que pasaría» y por debajo ya ha pasado.
    """
    from app.models import Checkin, DailyMetrics, RuleState
    from app.runner import pensar_el_dia

    antes = {
        t: db.scalar(select(func.count()).select_from(t))
        for t in (Decision, Checkin, Notification, HevyWrite, DailyMetrics, RuleState)
    }

    pensado = pensar_el_dia(db, cfg, LUNES, metrics=metricas(), rides=[])
    db.flush()

    assert pensado.decision is not None
    assert pensado.decision.light in {"green", "amber", "red"}
    assert pensado.decision.session is not None

    despues = {
        t: db.scalar(select(func.count()).select_from(t))
        for t in (Decision, Checkin, Notification, HevyWrite, DailyMetrics, RuleState)
    }
    assert despues == antes, (
        f"pensar el día ha escrito en la base: {antes} -> {despues}. Una "
        f"previsualización que guarda no es una previsualización"
    )


def test_pensar_el_dia_con_respuestas_que_no_estan_guardadas(db, cfg):
    """El dato de la previsualización viene del formulario, no de la base.

    Es lo que separa esta función de `run_daily`: allí el check-in se lee con
    `repo.get_checkin` porque ya está enviado, y aquí todavía no lo está. Sin
    esto, previsualizar enseñaría la decisión de las respuestas de la última
    vez, que es justo la pregunta que no se está haciendo.
    """
    from app.runner import pensar_el_dia

    tranquilo = pensar_el_dia(
        db, cfg, LUNES, metrics=metricas(), rides=[],
        respuestas=dict(CHECKIN_VERDE),
    )
    hecho_polvo = pensar_el_dia(
        db, cfg, LUNES, metrics=metricas(), rides=[],
        respuestas=dict(CHECKIN_ROJO),
    )

    assert tranquilo.decision.light != hecho_polvo.decision.light, (
        "las mismas métricas con respuestas opuestas han dado el mismo "
        "semáforo: las respuestas inyectadas no están llegando al motor"
    )
    # Y no se ha guardado ninguna de las dos, que es la otra mitad.
    from app.models import Checkin

    assert db.scalars(select(Checkin)).first() is None


def test_pensar_el_dia_sin_respuestas_lee_las_que_haya_guardadas(db, cfg):
    """`respuestas=None` no es `respuestas={}`, y la diferencia importa.

    `None` es «no me han dado ninguna, mira en la base», que es lo que necesita
    `run_daily` cuando el check-in ya está enviado. Un diccionario vacío es «hoy
    no hay respuestas», que es la mañana sin check-in. Aplastar los dos casos
    dejaría la previsualización leyendo el formulario de ayer.
    """
    from app.runner import pensar_el_dia

    upsert_checkin(db, LUNES, dict(CHECKIN_ROJO), config=cfg)
    db.flush()

    leido = pensar_el_dia(db, cfg, LUNES, metrics=metricas(), rides=[])
    vacio = pensar_el_dia(
        db, cfg, LUNES, metrics=metricas(), rides=[], respuestas={}
    )

    assert leido.decision.light != vacio.decision.light, (
        "con un check-in rojo guardado, leerlo y no leerlo tiene que dar "
        "semáforos distintos"
    )


def test_el_detector_de_nivel_alarga_la_ventana_de_wellness(cfg_copia):
    """Los 180 dias de referencia hay que TENERLOS delante para medirlos.

    Y no son 180 sino 187: la linea base del dia mas antiguo de la ventana mira
    los siete ANTERIORES a el, igual que pasa con `DIAS_DE_HISTORIA`. Sin este
    sumando el detector mediria contra media distribucion sin decir nada,
    porque media distribucion sigue pasando el minimo de cobertura.
    """
    from app.runner import dias_de_wellness_en_memoria

    sin = dias_de_wellness_en_memoria(cfg_copia)
    cfg_copia.raw["trend"]["nivel"] = {
        "historico_dias": 180, "reciente_dias": 30,
        "percentil_max": 12, "dias_min": 4,
    }
    con = dias_de_wellness_en_memoria(cfg_copia)
    assert con == 180 + cfg_copia.raw["baseline"]["window_days"]
    assert con > sin


def test_apagar_la_tendencia_tambien_apaga_la_ventana_del_detector_de_nivel(cfg_copia):
    """`enabled: false` no puede dejar cargando seis meses de wellness cada
    manana para una capa que no va a hablar."""
    from app.engine.signals import DIAS_DE_HISTORIA
    from app.runner import dias_de_wellness_en_memoria

    cfg_copia.raw["trend"]["nivel"] = {
        "historico_dias": 180, "reciente_dias": 30,
        "percentil_max": 12, "dias_min": 4,
    }
    cfg_copia.raw["trend"]["enabled"] = False
    esperado = DIAS_DE_HISTORIA + cfg_copia.raw["baseline"]["window_days"]
    assert dias_de_wellness_en_memoria(cfg_copia) == esperado


# ---------------------------------------------------------------------------
# `_cumplimiento_contra`: los dos veredictos de la noche
# ---------------------------------------------------------------------------
#
# Desde el 25/09/2026 la noche saca DOS veredictos del mismo bucle: el estricto
# -que exige tambien el peso de cada serie y alimenta la racha de sesiones
# limpias- y el de subir -que no lo exige, porque una serie mas ligera con las
# reps completas es el escalon de abajo de una rampa y no un fallo-.
#
# Se calculan juntos a proposito: el criterio de union de una sesion partida en
# dos ratos (el OR del cumplimiento, la preferencia de motivo sobre SIN_RASTRO)
# tiene que ser el mismo para los dos, y escribirlo dos veces es la forma de que
# se separen sin que nada falle.


def _plan_de_una_serie(peso: float, reps: int = 24) -> dict:
    return {
        "exercises": [{
            "key": "patada_atras",
            "name": "Patada atrás",
            "template_id": "T1",
            "sets": [{"reps": reps, "weight_kg": peso}],
        }]
    }


def _entreno(peso: float, reps: int = 24, wid: str = "w1") -> dict:
    return {
        "id": wid,
        "start_time": f"{LUNES.isoformat()}T07:30:00Z",
        "exercises": [{
            "exercise_template_id": "T1",
            "sets": [{"type": "normal", "reps": reps, "weight_kg": peso}],
        }],
    }


def test_la_noche_saca_dos_veredictos_y_no_dicen_lo_mismo(cfg):
    """Una serie mas ligera con las reps completas: estricto NO, subir SI."""
    from app.runner import _cumplimiento_contra

    c = _cumplimiento_contra([_entreno(30)], _plan_de_una_serie(35), cfg)
    assert c.executed["patada_atras"] is False
    assert c.sube["patada_atras"] is True
    assert "30 kg" in c.motivos["patada_atras"]
    # Y para subir no hay nada que explicar: el motivo se filtra con SU
    # veredicto. Con el estricto, aqui quedaria la frase del peso pegada a un
    # ejercicio que para subir ya cuenta como limpio.
    assert "patada_atras" not in c.motivos_sube


def test_los_dos_veredictos_coinciden_cuando_lo_corto_son_las_reps(cfg):
    """La pareja: ignorar el peso no perdona una serie que se corto."""
    from app.runner import _cumplimiento_contra

    c = _cumplimiento_contra([_entreno(40, reps=4)], _plan_de_una_serie(35), cfg)
    assert c.executed["patada_atras"] is False
    assert c.sube["patada_atras"] is False
    assert "reps" in c.motivos_sube["patada_atras"]


def test_una_sesion_partida_en_dos_ratos_se_une_igual_en_los_dos(cfg):
    """El rato bueno rescata al malo, y tiene que hacerlo en los DOS veredictos.

    Primer rato ligero y completo de reps, segundo rato al peso pedido. El
    estricto se salva por el segundo; el de subir ya estaba limpio con el
    primero. Si la union se escribiera dos veces, aqui es donde se separarian.
    """
    from app.runner import _cumplimiento_contra

    c = _cumplimiento_contra(
        [_entreno(30, wid="w1"), _entreno(35, wid="w2")],
        _plan_de_una_serie(35),
        cfg,
    )
    assert c.executed["patada_atras"] is True
    assert c.sube["patada_atras"] is True
    assert c.motivos == {} and c.motivos_sube == {}
    # Y el peso que viaja a la adopcion es el mas alto de los dos ratos.
    assert c.pesos["patada_atras"] == 35.0


def test_la_ultima_corta_se_mantiene_y_un_rato_limpio_la_convierte_en_limpia(cfg):
    """Mantener es la excepción de un fallo, no un tercer veredicto que compita
    con el limpio: si en otro rato del día el ejercicio salió entero, suma."""
    from app.runner import _cumplimiento_contra

    sola = _cumplimiento_contra([_entreno(35, reps=20)], _plan_de_una_serie(35), cfg)
    assert sola.mantiene == {"patada_atras"}
    assert sola.executed["patada_atras"] is False

    partida = _cumplimiento_contra(
        [_entreno(35, reps=20, wid="w1"), _entreno(35, reps=24, wid="w2")],
        _plan_de_una_serie(35),
        cfg,
    )
    assert partida.executed["patada_atras"] is True
    assert partida.mantiene == set()


def test_de_una_sesion_partida_se_adoptan_las_series_del_rato_mas_pesado(cfg):
    """Las series de UN rato, el de la serie más pesada, y no las del último.

    Desde el 25/09/2026 la adopción copia la forma hecha, no solo el tope. Si
    se quedaran las del último rato, un segundo rato más ligero borraría lo que
    se levantó en el primero; y si se mezclaran, la forma adoptada sería una que
    no se hizo de seguido nunca. El rato pesado va PRIMERO a propósito: con el
    orden al revés, «el último» y «el más pesado» coinciden y no se distinguen.
    """
    from app.runner import _cumplimiento_contra

    c = _cumplimiento_contra(
        [_entreno(45, wid="w1"), _entreno(35, wid="w2")],
        _plan_de_una_serie(35),
        cfg,
    )
    assert [s["weight_kg"] for s in c.series["patada_atras"]] == [45]
    assert c.pesos["patada_atras"] == 45.0, "`pesos` es el tope de esas series"


def test_el_motivo_de_subir_se_filtra_con_SU_veredicto_y_no_con_el_estricto(cfg):
    """Un rato corto de reps y otro completo: para subir no queda nada que decir.

    Es el unico caso en que los dos filtros difieren, y por eso hay que buscarlo
    a proposito. En una sesion suelta, `sube` falso implica `executed` falso, asi
    que filtrar por uno o por otro da lo mismo. Partida en dos ratos NO:

        rato 1: 30 kg x 4 reps   -> corto de reps, deja motivo en los dos
        rato 2: 30 kg x 24 reps  -> completo de reps, rescata `sube` por el OR

    Union: `executed` falso (ningun rato llego a los 35 kg) y `sube` VERDADERO.
    Filtrando los motivos de subir con `executed`, la frase de las reps del rato
    1 sobrevive pegada a un ejercicio que para subir ya cuenta como limpio: el
    mensaje de la manana explicaria un rechazo que no ha ocurrido.

    Lo cazo el banco de mutaciones del 25/09/2026; el primer test que se escribio
    para esto no lo distinguia.
    """
    from app.runner import _cumplimiento_contra

    c = _cumplimiento_contra(
        [_entreno(30, reps=4, wid="w1"), _entreno(30, reps=24, wid="w2")],
        _plan_de_una_serie(35),
        cfg,
    )
    assert c.executed["patada_atras"] is False
    assert c.sube["patada_atras"] is True
    assert "patada_atras" in c.motivos, "el estricto si tiene algo que explicar"
    assert c.motivos_sube == {}, (
        f"para subir no hay nada que explicar y quedo: {c.motivos_sube}"
    )


# ---------------------------------------------------------------------------
# «Lo hice y no lo apunté», del formulario de despues de entrenar
# ---------------------------------------------------------------------------


def _otro_entreno() -> dict:
    """Una sesión que existe pero que no contiene el ejercicio del plan."""
    return {
        "id": "w9",
        "start_time": f"{LUNES.isoformat()}T07:30:00Z",
        "exercises": [{
            "exercise_template_id": "T-OTRO",
            "title": "Otra cosa",
            "sets": [{"type": "normal", "reps": 10, "weight_kg": 20}],
        }],
    }


def test_lo_que_se_hizo_sin_apuntar_cuenta_como_hecho(cfg):
    """Un olvido de REGISTRO deja de castigarse como un entreno sin hacer.

    Sin esto, un ejercicio que no aparece en Hevy cuenta como incumplido y
    rompe la racha. Es lo prudente mientras nadie sepa qué pasó -si no está, o
    no se hizo o no se registró-, pero deja de serlo en cuanto el usuario lo
    contesta: entonces no es ausencia de dato, es un dato.
    """
    from app.runner import _cumplimiento_contra

    # Hay sesión -por eso hay veredicto- pero ESE ejercicio no está en ella.
    # Con la lista de entrenamientos vacía no hay veredicto de nada y el test
    # no probaría lo que dice: lo enseñó un KeyError al escribirlo.
    sin = _cumplimiento_contra([_otro_entreno()], _plan_de_una_serie(35), cfg)
    con = _cumplimiento_contra(
        [_otro_entreno()], _plan_de_una_serie(35), cfg, {"patada_atras"}
    )
    assert sin.executed["patada_atras"] is False
    assert "patada_atras" in sin.motivos
    assert con.executed["patada_atras"] is True
    assert con.sube["patada_atras"] is True
    assert con.motivos == {}, "lo que cuenta como hecho no tiene nada que explicar"


def test_lo_que_se_hizo_sin_apuntar_NO_inventa_un_peso(cfg):
    """Puede cerrar una racha; nunca subir una carga.

    De un ejercicio sin registrar no hay ni un kilo que leer, y ponerle uno
    sería fabricar el dato del que cuelga la adopción. La direccion segura en
    una espalda con hernia es esta: que la palabra del usuario valga para decir
    «lo hice» y no para decir «lo hice con 80».
    """
    from app.runner import _cumplimiento_contra

    c = _cumplimiento_contra(
        [_otro_entreno()], _plan_de_una_serie(35), cfg, {"patada_atras"}
    )
    assert c.pesos.get("patada_atras") is None


def test_un_ejercicio_que_no_estaba_en_el_plan_se_ignora(cfg):
    """El formulario habla de hoy y el plan puede haber cambiado entre medias.

    Inventarle una entrada al cumplimiento meteria en el veredicto del dia un
    ejercicio que ese dia no se pedia, y el veredicto es el que dice si la
    sesion fue completa.
    """
    from app.runner import _cumplimiento_contra

    c = _cumplimiento_contra(
        [_otro_entreno()], _plan_de_una_serie(35), cfg, {"un_ejercicio_de_otro_dia"}
    )
    assert "un_ejercicio_de_otro_dia" not in c.executed
    assert c.executed["patada_atras"] is False
