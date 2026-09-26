"""Utilidades compartidas por la batería de pruebas.

Dos decisiones de fondo:

1. **Se prueba contra el `config.yaml` real, no contra uno de juguete.** Un
   config de prueba pasaría los tests el día que el de verdad se rompa, que es
   exactamente el día en el que hacen falta. Cuando un test necesita variar algo
   se hace una copia profunda (`cfg_copia`) y se toca ahí.

2. **Ningún test toca la red ni el disco del usuario.** Lo que necesita disco
   usa `tmp_path`; lo que necesita HTTP usa el doble de `FakeHTTP`. Una batería
   que depende de que Garmin conteste no es una batería, es una apuesta.

La regla 2 dejó de ser una convención y pasó a estar cerrada con llave
(`sin_red`) el día que se comprobó que no bastaba: ver ahí abajo.
"""

from __future__ import annotations

import copy
import json
import socket
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.dobles import doble_de, no_es_doble


# ---------------------------------------------------------------------------
# El cerrojo
# ---------------------------------------------------------------------------


@no_es_doble("excepción propia del cerrojo de red; no representa nada de `app/`")
class RedProhibidaEnTests(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    """Corta la red a nivel de socket durante toda la batería.

    NO es paranoia de manual. Escribiendo `tests/test_api.py` se descubrió que
    `POST /api/checkin` construye los clientes de verdad con `_clientes(cfg)`,
    que el `.env` del usuario tiene todas las claves puestas y que `DRY_RUN`
    está a `false`. Es decir: un test de la API sobrescribió la rutina REAL del
    usuario en Hevy y le mandó mensajes REALES de Telegram con fechas de mentira.

    Ningún doble lo habría evitado, porque el fallo no estaba en un doble que
    faltara sino en una ruta que fabrica sus propios clientes por dentro. Por eso
    el corte va en el sitio más bajo posible -`socket.connect`- donde da igual
    qué capa lo intente: httpx, garminconnect o lo que se añada mañana.

    El error dice qué host se intentó, porque un `ConnectionError` pelado en
    mitad de la batería no se distingue de un test mal escrito.

    Se deja pasar el bucle local: el `ProactorEventLoop` de Windows se fabrica su
    tubería interna con un socket a 127.0.0.1, y `TestClient` lo necesita para
    arrancar. Cortarlo también tumbaría la batería entera sin proteger de nada,
    porque en 127.0.0.1 no hay ni Garmin ni Hevy ni Telegram.
    """
    real_connect = socket.socket.connect
    LOCALES = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}

    def prohibido(self, address, *args, **kwargs):  # noqa: ANN001
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host.split("%")[0] in LOCALES:
            return real_connect(self, address, *args, **kwargs)
        raise RedProhibidaEnTests(
            f"un test ha intentado abrir una conexión de red a {address!r}. "
            f"Los tests no hablan con Garmin, Hevy ni Telegram: si el código bajo "
            f"prueba necesita un cliente, hay que inyectarle un doble. Si esto "
            f"salta en una ruta de FastAPI, es que la ruta se fabrica el cliente "
            f"por dentro y hay que sustituir esa función en el test."
        )

    monkeypatch.setattr(socket.socket, "connect", prohibido)
    yield
    monkeypatch.setattr(socket.socket, "connect", real_connect)

from app.config_loader import load_config
from app.engine.signals import Checkin, DayMetrics, Ride, Signals

REPO_ROOT = Path(__file__).resolve().parents[1]

# Lunes de referencia. Con la variante `with_pool` activa: lunes = dia_1.
LUNES = date(2026, 9, 7)


@pytest.fixture(scope="session")
def cfg():
    """El `config.yaml` del repositorio, ya validado."""
    return load_config(REPO_ROOT / "config.yaml")


@pytest.fixture
def cfg_copia(cfg):
    """Copia profunda modificable, para los tests que necesitan variar el YAML."""
    return copy.deepcopy(cfg)


def con_bloque_hiit(cfg):
    """El config real con el HIIT del Día 1 en un BLOQUE APARTE, como estuvo
    hasta el 26/09/2026.

    Ese día los intervalos del Día 1 pasaron dentro del Día 1 y los bloques HIIT
    aparte se apagaron (`hiit.enabled: false`). El mecanismo sigue en el código
    -por si se vuelve a separar-, y sus tests necesitan un config que lo use.
    Se construye desde el real, sacando del Día 1 lo que `hiit.embedded` declara
    suyo: así el bloque de prueba son los ejercicios de verdad y no una copia
    que pueda quedarse vieja. `test_el_config_con_bloque_de_prueba_es_valido`
    comprueba que esto sigue siendo un config que arranca.
    """
    otro = copy.deepcopy(cfg)
    raw = otro.raw
    suyos = set(raw["hiit"]["embedded"].pop("dia_1"))
    dia_1 = raw["routines"]["dia_1"]
    raw["routines"]["hiit_dia_1"] = {
        "title": "Día 1 HIIT",
        "hevy_routine_id": "hiit-dia-1-de-prueba",
        "exercises": [e for e in dia_1["exercises"] if e["key"] in suyos],
    }
    dia_1["exercises"] = [e for e in dia_1["exercises"] if e["key"] not in suyos]
    raw["hiit"].update(
        enabled=True, start_week=1, program_start_date=None,
        allowed_routines=["dia_1"], blocks={"dia_1": "hiit_dia_1"}, only_on_green=True,
    )
    for luz, permitido in (("green", True), ("amber", False), ("red", False)):
        raw["actions"][luz]["allow_hiit"] = permitido
    for regla in raw.get("special_rules") or []:
        if regla.get("name") == "semana_de_descarga":
            regla["action"]["allow_hiit"] = True
    return otro


@pytest.fixture
def cfg_con_bloque(cfg):
    """Ver `con_bloque_hiit`."""
    return con_bloque_hiit(cfg)


# AQUÍ ESTABA `cfg_summer`. Era una copia del config con `active_variant` a
# "summer", y existía por una sola razón: la variante activa, `with_pool`, no
# programaba `dia_3` ningún día de la semana, así que cualquier test que
# necesitara esa rutina tenía que cambiarse de calendario para alcanzarla.
#
# Esa fixture era el síntoma escrito en los tests de un fallo que estaba en el
# config: el Día 3 se hace, y se hace a menudo, y el sistema no lo tenía
# programado nunca. Todas esas sesiones se registraron como entrenos sueltos.
# Con la rotación 1→2→3 las tres rutinas están en el ciclo por definición y a
# `dia_3` se llega poniendo el puntero donde toca -`EngineState(last_strength=
# ("dia_2", ...))`-, que además es lo que pasa de verdad.


# ---------------------------------------------------------------------------
# Constructores de datos sintéticos
# ---------------------------------------------------------------------------


def sig(day: date, history: dict[str, dict[date, Any]] | None = None, **values) -> Signals:
    """Un `Signals` a mano, sin pasar por `build_signals`.

    Ojo: `sig(day)` a secas NO es "un día normal", es un día en el que no se
    sabe absolutamente nada y las 13 reglas se quedan sin evaluar. Para "un día
    en el que el sistema tiene todo lo que necesita" está `sig_completa`.
    """
    return Signals(day=day, values=values, history=history or {})


# Todas las señales que pide alguna regla del `config.yaml` real, con valores
# tranquilos (día verde). Existe porque `sig(day)` se estaba usando como si
# fuera un día normal cuando es justo lo contrario, y eso hacía pasar tests que
# afirmaban "aquí no falta ningún dato" sobre un día en el que faltaban todos.
SENALES_COMPLETAS: dict[str, Any] = {
    "lower_discomfort": 1,
    "upper_discomfort": 1,
    "fatigue": 3,
    "training_desire": 8,
    "hrv": 60.0,
    "hrv_baseline": 60.0,
    "hrv_ratio": 1.0,
    "rhr": 50.0,
    "rhr_baseline": 50.0,
    "rhr_delta": 0.0,
    "sleep_min": 450.0,
    "load_2d": 100.0,
    # Aquí estaban `weekend_intense_rides: 0` y `weekend_total_hours: 1.0`, y se
    # han ido. Eran las dos señales que pedía `resaca_finde`; la regla se borró
    # hace tiempo y `build_signals` dejó de escribirlas al pasar el recuento a
    # ventana rodante. Dejarlas aquí convertía este diccionario -que se llama
    # «todas las señales que pide alguna regla»- en una lista de señales que ya
    # no existen, y el test que lo vigila habría seguido en verde: comprueba que
    # no FALTE ninguna, no que no SOBRE ninguna.
}

# Los percentiles NO son señales: viven en `Signals.adaptive` y las reglas los
# leen por ahí (`gt_adaptive: load_2d_p90`), no en `values`. Ponerlos en
# `values` no da error, simplemente no los encuentra nadie.
UMBRALES_COMPLETOS: dict[str, float] = {
    "load_2d_p90": 200.0,
    "load_7d_p90": 400.0,
}


def sig_completa(day: date, *, dias_historico: int = 7, **overrides) -> Signals:
    """Un día en el que no falta ningún dato.

    Rellena las tres vías por las que una regla puede pedir algo, que no son
    intercambiables:

    - `values`, para el valor de hoy;
    - `adaptive`, para los percentiles;
    - `history`, porque una regla con `consecutive_days: 2` mira la serie y no
      el valor de hoy. Con el valor solo, la regla sigue sin poder evaluarse.

    El histórico repite el valor de hoy hacia atrás: una semana tranquila e
    igual a sí misma, que es lo que hace falta para que ninguna regla salte por
    accidente.

    `tests/test_message.py::test_el_dia_completo_no_deja_ninguna_regla_sin_evaluar`
    comprueba que sigue siendo cierto: el día que una regla nueva pida una señal
    que no esté aquí, ese test lo dice en vez de dejar que los demás sigan
    pasando por el motivo equivocado.
    """
    values = {**SENALES_COMPLETAS, **overrides}
    history = {
        k: {day - timedelta(days=i): v for i in range(dias_historico)}
        for k, v in values.items()
        if isinstance(v, (int, float))
    }
    s = sig(day, history=history, **values)
    s.adaptive.update(UMBRALES_COMPLETOS)
    return s


def eligiendo(s: Signals, eleccion: str) -> Signals:
    """El mismo día, con el selector del check-in contestado.

    No se pasa por `sig(day, chosen_session=...)`, y no es un descuido de la
    firma: eso lo metería en `values`, que es el espacio de nombres que ven las
    reglas y donde solo caben números y booleanos. Lo elegido vive en un campo
    propio de `Signals`, igual que `rides`, y este ayudante existe para que los
    tests lo pongan por el mismo sitio por el que lo pone `build_signals`.
    """
    s.sesion_elegida = eleccion
    return s


def ride(
    day: date,
    *,
    load: float | None = None,
    zones: tuple[float, ...] | None = None,
    duration_s: float = 3600.0,
    activity_id: int | None = None,
    cycling: bool = True,
) -> Ride:
    return Ride(
        date=day,
        duration_s=duration_s,
        distance_m=duration_s * 8,
        zones=zones,
        training_load=load,
        is_cycling=cycling,
        activity_id=activity_id,
    )


def dias(day: date, n: int, **kwargs) -> list[DayMetrics]:
    """`n` días de wellness terminando en `day`, todos con los mismos valores."""
    return [DayMetrics(date=day - timedelta(days=i), **kwargs) for i in range(n)]


def checkin(day: date, **values) -> Checkin:
    return Checkin(date=day, values=dict(values))


# ---------------------------------------------------------------------------
# Doble de httpx
# ---------------------------------------------------------------------------


@doble_de(httpx.Response)
class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self) -> Any:
        return self._payload


@doble_de(httpx.Client)
class FakeHTTP:
    """Sustituto de `httpx.Client` que registra lo que se le pide.

    No simula la red: simula el contrato mínimo que usan `hevy.py` y
    `telegram.py`. Si esos módulos empiezan a usar más superficie de httpx,
    este doble fallará ruidosamente en vez de fingir que todo va bien.
    """

    def __init__(self, respuestas: list[FakeResponse] | None = None):
        self.respuestas = list(respuestas or [])
        self.llamadas: list[dict[str, Any]] = []
        self.cerrado = False

    # -- protocolo de contexto ------------------------------------------------
    def __enter__(self) -> "FakeHTTP":
        return self

    def __exit__(self, *exc) -> bool:
        self.cerrado = True
        return False

    # -- verbos ---------------------------------------------------------------
    def _siguiente(self, verbo: str, url: str, **kwargs) -> FakeResponse:
        self.llamadas.append({"verb": verbo, "url": url, **kwargs})
        if not self.respuestas:
            raise AssertionError(
                f"{verbo.upper()} {url}: el doble se ha quedado sin respuestas "
                f"preparadas. El código bajo prueba hizo más peticiones de las "
                f"esperadas."
            )
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self._siguiente("get", url, **kwargs)

    def put(self, url: str, **kwargs) -> FakeResponse:
        if "/v1/routines/" in url:
            rechazo = _hevy_rechazaria(kwargs.get("json"))
            if rechazo is not None:
                # La llamada se registra igual: un test que quiera ver qué se
                # intentó mandar tiene que poder mirarlo aunque se rechazara.
                self.llamadas.append({"verb": "put", "url": url, **kwargs})
                return rechazo
        return self._siguiente("put", url, **kwargs)

    def post(self, url: str, **kwargs) -> FakeResponse:
        if "/sendMessage" in url:
            rechazo = _telegram_rechazaria(kwargs.get("json"))
            if rechazo is not None:
                self.llamadas.append({"verb": "post", "url": url, **kwargs})
                return rechazo
        return self._siguiente("post", url, **kwargs)


# Claves que Hevy DEVUELVE en el GET y RECHAZA en el PUT con un 400.
_PROHIBIDAS_EJERCICIO = {"index", "title"}
_PROHIBIDAS_SERIE = {"index"}


def _tipo_json(valor: Any) -> str:
    """El nombre que le da Hevy al tipo recibido, que es el de JavaScript.

    `[]` es "array" y no "list", `None` es "null" y no "NoneType". Importa
    porque el texto del 400 se compara en algún test, y un doble que invente el
    vocabulario del error vuelve a ser un doble que se prueba a sí mismo.
    """
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "boolean"
    if isinstance(valor, (list, tuple)):
        return "array"
    if isinstance(valor, dict):
        return "object"
    if isinstance(valor, (int, float)):
        return "number"
    return "string"


def _texto_o_400(valor: Any, presente: bool) -> FakeResponse | None:
    """La validación de un campo de texto, tal y como la hace Hevy."""
    if not presente:
        return FakeResponse(400, text=json.dumps({"error": "Required"}))
    if valor is None or isinstance(valor, str):
        return None
    return FakeResponse(
        400,
        text=json.dumps(
            {"error": f"Expected string, received {_tipo_json(valor)}"}
        ),
    )


def _numero_rechazado(valor: Any) -> bool:
    """¿Este valor da «received nan»? Ojo: casi ninguno.

    Hevy NO valida los campos numéricos, los COERCIONA con el `Number()` de
    JavaScript y solo se queja cuando el resultado es `NaN`. Medido:
    `"8"` pasa, `true` pasa, `[]` pasa (y vale 0). Lo único que salta es un
    texto no numérico. Esto se modela tal cual -permisivo- a propósito: si el
    doble fuera más estricto que la API, un test podría pasar por un 400 que en
    la realidad no existe, y el código real escribiría ceros en la rutina
    creyéndose protegido por un rechazo que nadie va a mandar.
    """
    if valor is None or isinstance(valor, (bool, int, float)):
        return False
    if isinstance(valor, str):
        try:
            float(valor.strip() or "0")
        except ValueError:
            return True
        return False
    if isinstance(valor, (list, tuple)):
        # `Number([])` es 0 y `Number([7])` es 7; con dos o más elementos es NaN.
        return len(valor) > 1
    return True


_CAMPOS_NUMERICOS_SERIE = ("weight_kg", "reps", "distance_meters", "duration_seconds")


def _hevy_rechazaria(cuerpo: Any) -> FakeResponse | None:
    """El 400 de verdad de Hevy, reproducido aquí.

    POR QUÉ ESTÁ ESTO. El 2026-09-12 se descubrió que la reversión de rutinas
    estaba rota desde siempre: `restore` reenviaba la copia -que es la respuesta
    del GET tal cual, con `index` y `title`- y Hevy contestaba
    `400 Unrecognized key(s) in object: 'index'`. Tenía seis tests y los seis
    pasaban, porque este doble aceptaba cualquier cuerpo que le dieran.

    Esa es la moraleja cara: un doble permisivo no prueba un contrato, prueba
    que el código hace lo que hace. Los tests comprobaban la lógica de la
    reversión -que elige la copia correcta, que respeta el interruptor- y ni uno
    podía fallar por el motivo por el que la función fallaba de verdad.

    Así que el doble ya no acepta cualquier cosa: rechaza exactamente lo que
    rechaza Hevy. Si alguien vuelve a mandar la forma del GET en un PUT, se
    entera aquí y no en la primera escritura real.

    Y VOLVIÓ A PASAR EL 2026-09-14, POR EL HUECO DE AL LADO
    -------------------------------------------------------
    Aquí solo se miraban NOMBRES de clave. Los tipos no los miraba nadie, ni
    aquí ni en el código, y por ahí se fue una mañana entera: el motor mandó
    `"notes": []` -porque `BuiltSession.notes` es una `list[str]`- en un campo
    de texto, Hevy contestó 400 y la rutina se quedó como estaba desde el 8 de
    septiembre. Una lista blanca de nombres es la mitad de un contrato.

    LO QUE HAY AQUÍ ESTÁ MEDIDO, NO RECORDADO, igual que en el doble de
    Telegram y por la misma razón. `scripts/sondeo_contrato_hevy.py` lo sondea
    contra la API real usando un `routine_id` inexistente -Hevy valida el
    cuerpo ANTES de buscar la rutina, así que un cuerpo bueno llega al 404 y uno
    malo se queda en el 400, sin tocar nada-. El 2026-09-14:

        notes = []              -> 400 Expected string, received array
        notes = 5               -> 400 Expected string, received number
        title = ['x']           -> 400 Expected string, received array
        title ausente           -> 400 Required
        notes del ejercicio=[]  -> 400 Expected string, received array
        index en el ejercicio   -> 400 Unrecognized key(s) in object: 'index'
        reps = 'ocho'           -> 400 Expected number, received nan
        reps = True             -> ACEPTADO (vale 1)
        reps = '8'              -> ACEPTADO (vale 8)
        rest_seconds = '90'     -> ACEPTADO (vale 90)
        weight_kg = []          -> ACEPTADO (vale 0)

    Las cuatro últimas son las importantes y el doble las ACEPTA, aunque duela:
    los campos numéricos no se validan, se coercionan con el `Number()` de
    JavaScript. Un `weight_kg` mal tipado no da error, se escribe como CERO
    KILOS. Fingir aquí un rechazo que la API no manda sería el mismo pecado que
    aceptarlo todo, solo que en la otra dirección: haría creer que la red
    protege de algo de lo que no protege, y quien lea estos tests sacaría la
    conclusión contraria a la verdadera. De eso protege `_es_escalar`, que es
    local y se ejecuta antes de enviar; y hay un test que lo dice en voz alta.
    """
    if not isinstance(cuerpo, dict):
        return None
    r = cuerpo.get("routine")
    if not isinstance(r, dict):
        return FakeResponse(
            400, text='{"error":"Expected object at routine, received undefined"}'
        )

    # Los campos de texto de la rutina, que sí se validan.
    for campo in ("title", "notes"):
        fallo = _texto_o_400(r.get(campo), campo in r)
        if fallo is not None:
            return fallo

    malas: set[str] = set()
    for ex in r.get("exercises") or []:
        if not isinstance(ex, dict):
            continue
        malas |= set(ex) & _PROHIBIDAS_EJERCICIO
        for s in ex.get("sets") or []:
            if isinstance(s, dict):
                malas |= set(s) & _PROHIBIDAS_SERIE
    if malas:
        detalle = ". ".join(
            f"Unrecognized key(s) in object: {k!r}" for k in sorted(malas)
        )
        return FakeResponse(400, text=json.dumps({"error": detalle}))

    # Las claves prohibidas van ANTES que los tipos del ejercicio porque es el
    # orden en el que contesta Hevy: con `index` puesto y `notes` mal a la vez,
    # el 400 que llega es el de `index`. Un doble que los ordenara al revés
    # haría pasar un test que afirma qué mensaje concreto se recibe.
    for ex in r.get("exercises") or []:
        if not isinstance(ex, dict):
            continue
        if "notes" in ex:
            fallo = _texto_o_400(ex.get("notes"), True)
            if fallo is not None:
                return fallo
        for s in ex.get("sets") or []:
            if not isinstance(s, dict):
                continue
            for campo in _CAMPOS_NUMERICOS_SERIE:
                if campo in s and _numero_rechazado(s[campo]):
                    return FakeResponse(
                        400,
                        text=json.dumps(
                            {"error": "Expected number, received nan"}
                        ),
                    )
    return None


# Las etiquetas que el `parse_mode=HTML` de Telegram acepta. La lista es la de
# su documentación; aquí importan `b`, `i` y `code`, que son las que escribe
# `engine/message.py`. Las demás están para que el doble no invente un fallo si
# algún día se usa una.
_TG_ETIQUETAS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "span", "tg-spoiler", "blockquote",
}


def _telegram_rechazaria(cuerpo: Any) -> FakeResponse | None:
    """El 400 de verdad de Telegram cuando no sabe leer el HTML.

    POR QUÉ ESTÁ ESTO. Mismo cuento que `_hevy_rechazaria` y descubierto por
    tirar del mismo hilo. El doble aceptaba cualquier texto, así que ningún test
    podía fallar por el motivo por el que el envío falla de verdad: Telegram
    valida el HTML, lo valida ANTES de mirar si el chat existe, y si no lo sabe
    leer NO manda nada. El sitio donde eso dolía era el aviso de trabajo
    fallido, que interpolaba `str(excepción)` dentro de `<code>` sin escapar: un
    `TypeError: '<' not supported between instances of...` tumbaba el único
    aviso que no tiene freno, justo el día que hacía falta.

    LO QUE HAY AQUÍ ESTÁ MEDIDO, NO RECORDADO. Contra la API real, con un
    `chat_id` inválido para que no le llegara nada a nadie:

        <foo>mundo</foo>      -> Unsupported start tag "foo" at byte offset 5
        TypeError: '<' not..  -> Unsupported start tag "'" at byte offset 12
        5 < 7                 -> Unsupported start tag "" at byte offset 2
        hola <b>mundo         -> Can't find end tag corresponding to start tag "b"
        hola </b>mundo        -> Unexpected end tag at byte offset 5
        <b>a<i>b</b>c</i>     -> Unmatched end tag at byte offset 8, expected...

    Y dos que SÍ pasan, que son tan informativas como las que fallan:

        Tom & Jerry           -> llega a "chat not found"
        algo &fo; mas         -> llega a "chat not found"

    O sea que el `&` suelto Telegram lo perdona y el `<` no lo perdona nunca.
    `escapar_html` escapa el `&` igualmente, pero por otro motivo: sin eso, un
    texto que contenga literalmente "&lt;" se leería como un "<" que nadie
    escribió. Es corrección de lo que se muestra, no del transporte.

    Lo que este doble NO modela: atributos mal formados en `<a href=...>`,
    `<pre>` con lenguaje, y el resto de la superficie que este proyecto no
    escribe. Si algún día se escriben, esto se queda corto y hay que volver a
    medirlo contra la API, no ampliarlo a ojo.
    """
    if not isinstance(cuerpo, dict):
        return None
    if cuerpo.get("parse_mode") != "HTML":
        # Sin `parse_mode` no hay nada que parsear: es exactamente el reintento
        # en plano, y tiene que poder salir aunque el texto lleve ángulos.
        return None
    texto = cuerpo.get("text")
    if not isinstance(texto, str):
        return None

    def _mal(descripcion: str) -> FakeResponse:
        return FakeResponse(
            400,
            {"ok": False, "error_code": 400, "description": descripcion},
            text=json.dumps({"ok": False, "error_code": 400,
                             "description": descripcion}),
        )

    abiertas: list[tuple[str, int]] = []
    i = 0
    while True:
        i = texto.find("<", i)
        if i < 0:
            break
        # Telegram cuenta el desplazamiento en BYTES utf-8, no en caracteres.
        # Con emojis en la cabecera del mensaje, los dos números no coinciden.
        pos = len(texto[:i].encode("utf-8"))
        resto = texto[i + 1:]
        cierre = resto.startswith("/")
        if cierre:
            resto = resto[1:]
        # El nombre es lo que va hasta el primer espacio o hasta el '>'. No es
        # un parser de HTML: es lo que se dedujo de las respuestas de arriba,
        # donde `<'` dio nombre "'" y `< ` dio nombre "".
        nombre = ""
        for ch in resto:
            if ch in " \t\n>":
                break
            nombre += ch
        if nombre not in _TG_ETIQUETAS:
            return _mal(
                f"Bad Request: can't parse entities: Unsupported start tag "
                f'"{nombre}" at byte offset {pos}'
            )
        if cierre:
            if not abiertas:
                return _mal(
                    f"Bad Request: can't parse entities: Unexpected end tag "
                    f"at byte offset {pos}"
                )
            esperada, _ = abiertas.pop()
            if esperada != nombre:
                return _mal(
                    f"Bad Request: can't parse entities: Unmatched end tag at "
                    f'byte offset {pos}, expected "</{esperada}>", found '
                    f'"</{nombre}>"'
                )
        else:
            abiertas.append((nombre, pos))
        i += 1

    if abiertas:
        return _mal(
            f"Bad Request: can't parse entities: Can't find end tag "
            f'corresponding to start tag "{abiertas[-1][0]}"'
        )
    return None
