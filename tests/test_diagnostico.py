"""Los tres botones de comprobación: que digan la verdad, incluso la incómoda.

QUÉ SE PRUEBA AQUÍ Y QUÉ NO
---------------------------
No se prueba que Garmin responda ni que Hevy exista: eso se comprueba contra
los servicios de verdad, y se ha hecho. Lo que se prueba aquí es lo único que
puede estar mal sin que nadie lo note: **el informe**.

Un botón de diagnóstico que se pinta verde cuando algo falla es peor que no
tener botón, porque sustituye una duda por una certeza falsa. Así que la
batería va casi entera contra esa forma de fallar: cadenas cortadas que se
pintan enteras, pasos que no se hicieron contados como buenos, y avisos que se
calculan antes de que exista lo que tienen que avisar.
"""

from __future__ import annotations

from datetime import date, timedelta

from app import diagnostico as dg
from app.diagnostico import DIAS_ATRAS_LECTURA, Paso, Resultado
from tests.dobles import doble_de
from app.config_loader import Config
from app.engine.signals import DayMetrics
from app.integrations.garmin import GarminClient
from app.integrations.hevy import HevyClient
from app.integrations.telegram import TelegramClient


# ---------------------------------------------------------------------------
# El informe
# ---------------------------------------------------------------------------


def test_un_paso_que_no_se_pudo_hacer_no_cuenta_como_bueno():
    """`ok=None` es "no se intentó", y no se intentó no es "salió bien".

    Si contara como bueno, una cadena que se corta en el primer eslabón se
    pintaría verde entera: la manera más rápida de que un botón de diagnóstico
    mienta.

    OJO: este test NO sujeta él solo la diferencia entre `is True` y
    `is not False`, porque el paso en rojo ya basta para tumbar el informe y el
    `None` viaja de gorra. Eso lo sujeta el test de abajo. Se deja escrito
    porque es el caso real -primero revienta algo, luego lo que venía detrás no
    se intenta- y porque el que pasa por el motivo equivocado es el que nadie
    revisa después.
    """
    r = Resultado("X")
    r.paso("uno", False, "reventó")
    r.paso("dos", None, "no se intentó")
    assert r.ok is False


def test_un_paso_sin_intentar_entre_pasos_buenos_basta_para_no_pintar_verde():
    """Aquí el `None` es lo ÚNICO que decide, y por eso este test sí sirve.

    Sin ningún paso en rojo que lo tape, cambiar `p.ok is True` por
    `p.ok is not False` pintaría este informe de verde: un diagnóstico que dice
    "todo correcto" habiendo dejado un eslabón sin comprobar.
    """
    r = Resultado("X")
    r.paso("uno", True, "bien")
    r.paso("dos", None, "no se intentó")
    assert r.ok is False


def test_una_lista_de_pasos_vacia_no_es_todo_correcto():
    """Sin pasos no hay comprobación, y sin comprobación no hay verde."""
    assert Resultado("X").ok is False


def test_el_resumen_nombra_el_fallo_en_vez_de_contar_cuantos_salieron_bien():
    """"3 de 5 pasos bien" obliga a abrir la ficha para saber qué pasa.

    Lo único que se lee de un vistazo tiene que ser la causa.
    """
    r = Resultado("Garmin")
    r.paso("credenciales", True, "configuradas")
    r.paso("sesión", False, "no se pudo entrar")
    d = r.como_dict()
    assert d["resumen"] == "Garmin: no se pudo entrar"
    assert "3 de" not in d["resumen"]


def test_el_resumen_enseña_el_primer_fallo_y_no_el_ultimo():
    """El primero es la causa; los de después suelen ser consecuencias."""
    r = Resultado("Garmin")
    r.paso("sesión", False, "no se pudo entrar")
    r.paso("lectura", False, "no vino nada")
    assert r.como_dict()["resumen"] == "Garmin: no se pudo entrar"


def test_todos_los_pasos_bien_es_lo_unico_que_pinta_verde():
    r = Resultado("Hevy")
    r.paso("credenciales", True, "ok")
    r.paso("lectura", True, "ok")
    assert r.ok is True
    assert r.como_dict()["resumen"] == "Hevy: todo correcto"


def test_el_paso_viaja_entero_al_cliente():
    """Si el error no cruza, el aviso obliga a entrar por SSH a buscarlo."""
    d = Paso("sesión", False, "no se pudo entrar", "429").como_dict()
    assert d == {
        "nombre": "sesión",
        "ok": False,
        "detalle": "no se pudo entrar",
        "error": "429",
    }


# ---------------------------------------------------------------------------
# Garmin
# ---------------------------------------------------------------------------


@doble_de(GarminClient)
class _ClienteFalso:
    """Un cliente de Garmin de mentira, con los estados que importan."""

    def __init__(self, **kw):
        self.session_resumed = kw.get("session_resumed", True)
        self.tokens_guardados = kw.get("tokens_guardados", None)
        self.rate_limit_events = list(kw.get("rate_limit_events", []))
        self.token_dir = "/app/data/garmin_tokens"
        self._al_leer = kw.get("al_leer")
        self._metricas = kw.get("metricas")

    def connect(self):
        return None

    def day_metrics(self, day):
        if self._al_leer is not None:
            self._al_leer(self)
        return self._metricas


@doble_de(DayMetrics)
class _Metricas:
    def __init__(self, **kw):
        self.hrv = kw.get("hrv")
        self.rhr = kw.get("rhr")
        self.sleep_min = kw.get("sleep_min")
        self.sleep_score = kw.get("sleep_score")
        self.body_battery = kw.get("body_battery")


def _con_cliente(monkeypatch, cliente):
    import app.integrations.garmin as g

    monkeypatch.setattr(g, "build_client", lambda *a, **k: cliente)


def _paso(d, nombre):
    return next((p for p in d["pasos"] if p["nombre"] == nombre), None)


def _completas():
    return _Metricas(hrv=60, rhr=52, sleep_min=420, sleep_score=80, body_battery=70)


def test_no_saber_si_hubo_login_no_se_dobla_a_login_nuevo(monkeypatch):
    """Tres estados, tres frases.

    "Hubo login" es una advertencia; "no he podido averiguarlo" es una pregunta
    abierta sobre la librería. Juntarlas pierde justo la información que haría
    falta para saber si hay que ir a mirar el cliente.
    """
    _con_cliente(
        monkeypatch,
        _ClienteFalso(session_resumed=None, metricas=_completas()),
    )
    d = dg.probar_garmin(object(), None)
    detalle = _paso(d, "sesión")["detalle"]
    assert "no se ha podido saber" in detalle
    assert "login nuevo" not in detalle


def test_una_sesion_reanudada_dice_que_repetir_sale_gratis(monkeypatch):
    _con_cliente(
        monkeypatch,
        _ClienteFalso(session_resumed=True, metricas=_completas()),
    )
    d = dg.probar_garmin(object(), None)
    assert "reanudada" in _paso(d, "sesión")["detalle"]


def test_un_login_nuevo_avisa_de_no_insistir(monkeypatch):
    """Es el aviso que evita que la comprobación se convierta en la avería."""
    _con_cliente(
        monkeypatch,
        _ClienteFalso(session_resumed=False, metricas=_completas()),
    )
    d = dg.probar_garmin(object(), None)
    assert "no repetir" in _paso(d, "sesión")["detalle"]


def test_un_login_que_no_guardo_la_sesion_pinta_rojo(monkeypatch):
    """Leer bien hoy y no guardar nada no es "todo correcto".

    Es el fallo que se repite cada mañana sin dar la cara, y el botón existe
    sobre todo para esto.
    """
    _con_cliente(
        monkeypatch,
        _ClienteFalso(
            session_resumed=False, tokens_guardados=False, metricas=_completas()
        ),
    )
    d = dg.probar_garmin(object(), None)
    assert d["ok"] is False
    assert _paso(d, "tokens")["ok"] is False
    assert "repetirá el login" in _paso(d, "tokens")["detalle"]


def test_sin_login_no_se_afirma_nada_sobre_los_tokens(monkeypatch):
    """Si la sesión se reanudó no había nada que guardar.

    Un paso "tokens: bien" aquí sería una comprobación que no se ha hecho.
    """
    _con_cliente(
        monkeypatch,
        _ClienteFalso(
            session_resumed=True, tokens_guardados=None, metricas=_completas()
        ),
    )
    d = dg.probar_garmin(object(), None)
    assert _paso(d, "tokens") is None


def test_los_429_de_la_lectura_tambien_salen_en_el_informe(monkeypatch):
    """EL PASO VA AL FINAL, Y ESTO LO SUJETA.

    Los 429 no solo salen del login: cada lectura de wellness puede topar con
    el límite y reintentar por dentro. Montado justo después de conectar, este
    paso contaría los del login y se perdería los de la lectura, que son los
    que convierten una respuesta aparentemente limpia en una que ha costado
    cinco intentos.
    """
    cliente = _ClienteFalso(
        session_resumed=True,
        metricas=_completas(),
        al_leer=lambda c: c.rate_limit_events.append("429 en sleep"),
    )
    _con_cliente(monkeypatch, cliente)
    d = dg.probar_garmin(object(), None)

    limite = _paso(d, "límite de Garmin")
    assert limite is not None, (
        "el 429 ocurrió durante la lectura y no aparece: el paso se está "
        "calculando antes de que exista lo que tiene que contar"
    )
    assert limite["ok"] is False
    assert d["ok"] is False


def test_una_lectura_fallida_sigue_contando_sus_429(monkeypatch):
    """Cuando la lectura falla es cuando más importa saber si fue el límite."""

    def revienta(c):
        c.rate_limit_events.append("429 en hrv")
        raise RuntimeError("429 Too Many Requests")

    _con_cliente(
        monkeypatch, _ClienteFalso(session_resumed=True, al_leer=revienta)
    )
    d = dg.probar_garmin(object(), None)
    assert _paso(d, "lectura")["ok"] is False
    assert _paso(d, "límite de Garmin") is not None


def test_sin_429_no_se_inventa_el_paso(monkeypatch):
    """Un informe limpio no lleva una fila que diga "0 limitaciones"."""
    _con_cliente(
        monkeypatch, _ClienteFalso(session_resumed=True, metricas=_completas())
    )
    d = dg.probar_garmin(object(), None)
    assert _paso(d, "límite de Garmin") is None
    assert d["ok"] is True


def test_conectar_y_no_traer_nada_es_un_fallo_y_no_un_exito(monkeypatch):
    """La sesión vale y los datos no llegan: justo lo que el botón busca."""
    _con_cliente(
        monkeypatch,
        _ClienteFalso(session_resumed=True, metricas=_Metricas()),
    )
    d = dg.probar_garmin(object(), None)
    assert d["ok"] is False
    assert "no trajo ningún dato" in _paso(d, "lectura")["detalle"]


def test_un_hueco_suelto_no_tumba_la_lectura(monkeypatch):
    """Que falte el Body Battery de una noche es un dato, no una avería."""
    _con_cliente(
        monkeypatch,
        _ClienteFalso(
            session_resumed=True,
            metricas=_Metricas(hrv=60, rhr=52, sleep_min=420, sleep_score=80),
        ),
    )
    d = dg.probar_garmin(object(), None)
    assert d["ok"] is True
    assert "sin Body Battery" in _paso(d, "lectura")["detalle"]


def test_se_lee_un_dia_pasado_y_no_el_de_hoy(monkeypatch):
    """El wellness de hoy puede estar a medias porque el reloj no ha sincronizado.

    Un hueco ahí se leería como "Garmin no responde" cuando lo que pasa es que
    son las ocho de la mañana.
    """
    pedidos = []
    cliente = _ClienteFalso(session_resumed=True, metricas=_completas())
    cliente.day_metrics = lambda dia: (pedidos.append(dia), _completas())[1]
    _con_cliente(monkeypatch, cliente)
    dg.probar_garmin(object(), None)

    assert pedidos == [date.today() - timedelta(days=DIAS_ATRAS_LECTURA)]
    assert pedidos[0] != date.today()


def test_si_no_hay_cliente_los_pasos_siguientes_dicen_que_no_se_intentaron(
    monkeypatch,
):
    import app.integrations.garmin as g

    def revienta(*a, **k):
        raise RuntimeError("faltan credenciales")

    monkeypatch.setattr(g, "build_client", revienta)
    d = dg.probar_garmin(object(), None)

    assert d["ok"] is False
    assert [p["ok"] for p in d["pasos"]] == [False, None, None]


# ---------------------------------------------------------------------------
# Hevy
# ---------------------------------------------------------------------------


@doble_de(HevyClient)
class _HevyFalso:
    """Un Hevy de mentira con la alarma puesta en las puertas QUE EXISTEN.

    LA ALARMA VIGILABA UNA PUERTA TAPIADA
    -------------------------------------
    Aquí había `update_routine` y `create_routine`, y ninguno de los dos existe
    en `HevyClient`. O sea que `test_el_diagnostico_de_hevy_no_escribe_nada` no
    probaba nada: el diagnóstico no podía llamar a esos métodos ni queriendo,
    porque el cliente de verdad no los tiene. Un test en verde durante meses
    certificando que no se cruza una puerta tapiada, mientras las tres puertas
    de verdad -`write_routine`, `restore`, `revert_to_day_start`- se quedaban
    sin vigilar.

    Ahora las alarmas están en los tres métodos que el cliente real tiene y que
    de verdad escriben. Si el diagnóstico llama a cualquiera, el test lo caza.
    Y si mañana `HevyClient` estrena un cuarto método de escritura, esta clase
    no lo sabrá -eso sigue necesitando pensar-, pero al menos lo que hay escrito
    aquí ya no puede ser mentira: `tests/test_dobles.py` compara los nombres.
    """

    def __init__(self, rutinas, workouts=None):
        self._rutinas = rutinas
        self._workouts = workouts if workouts is not None else [1, 2, 3]
        self.escrituras = []

    def get_workouts(self, since=None, *, max_pages: int = 5):
        return self._workouts

    def get_routine(self, routine_id):
        if routine_id not in self._rutinas:
            raise RuntimeError("404 no existe")
        return {"title": self._rutinas[routine_id]}

    # -- las tres puertas por las que se escribe de verdad --------------------
    def write_routine(self, routine_id, payload, *, dry_run=False):
        self.escrituras.append("write_routine")
        raise AssertionError("el diagnóstico ha escrito en Hevy")

    def restore(self, routine_id, backup=None):
        self.escrituras.append("restore")
        raise AssertionError("el diagnóstico ha escrito en Hevy")

    def revert_to_day_start(self, routine_id, dia):
        self.escrituras.append("revert_to_day_start")
        raise AssertionError("el diagnóstico ha escrito en Hevy")


@doble_de(Config)
class _CfgFalso:
    def __init__(self, rutinas):
        self.routines = rutinas


def _con_hevy(monkeypatch, cliente):
    import app.integrations.hevy as h

    monkeypatch.setattr(h, "build_client", lambda *a, **k: cliente)


def test_el_diagnostico_de_hevy_no_escribe_nada(monkeypatch):
    """Una "prueba de escritura" dejaría la rutina del lunes como nadie pidió.

    El día que fallara a la mitad, el estropicio sería obra del botón que
    estaba ahí para detectar estropicios.
    """
    cliente = _HevyFalso({"r1": "Día 1"})
    _con_hevy(monkeypatch, cliente)
    dg.probar_hevy(object(), _CfgFalso({"dia_1": {"hevy_routine_id": "r1"}}))
    assert cliente.escrituras == []


def test_un_titulo_que_no_coincide_no_es_un_fallo(monkeypatch):
    """LOS TÍTULOS NO SON DATO.

    Está medido en este proyecto que 4 de 14 entrenamientos tienen un título
    que miente sobre su propia rutina. Y contra la cuenta real, Hevy devuelve
    "Dia 1 HIIT" donde el YAML dice "Día 1 HIIT": validar el título habría dado
    un fallo inventado por una tilde. Lo que cuenta es el identificador.
    """
    _con_hevy(monkeypatch, _HevyFalso({"r1": "Otra cosa completamente"}))
    d = dg.probar_hevy(
        object(), _CfgFalso({"dia_1": {"hevy_routine_id": "r1"}})
    )
    assert d["ok"] is True
    informativo = _paso(d, "títulos (solo informativos)")
    assert informativo is not None
    assert "solo informativos" in informativo["nombre"], (
        "el título tiene que ir etiquetado como lo que es, o el día que no "
        "coincida alguien lo leerá como una avería"
    )


def test_una_rutina_que_ya_no_existe_se_caza_antes_del_lunes(monkeypatch):
    """Es lo que de verdad aporta esta comprobación.

    Un identificador que apunta a una rutina borrada desde el móvil no da la
    cara hasta la mañana en que toca escribirla, y entonces el día se queda sin
    su versión.
    """
    _con_hevy(monkeypatch, _HevyFalso({"r1": "Día 1"}))
    d = dg.probar_hevy(
        object(),
        _CfgFalso(
            {
                "dia_1": {"hevy_routine_id": "r1"},
                "dia_2": {"hevy_routine_id": "borrada"},
            }
        ),
    )
    assert d["ok"] is False
    assert _paso(d, "rutinas")["ok"] is False
    assert "borrada" in _paso(d, "rutinas")["error"]


def test_cero_entrenamientos_no_es_un_fallo_de_clave(monkeypatch):
    """La lista vacía es una respuesta, y hay que decirlo para que no asuste."""
    _con_hevy(monkeypatch, _HevyFalso({"r1": "Día 1"}, workouts=[]))
    d = dg.probar_hevy(
        object(), _CfgFalso({"dia_1": {"hevy_routine_id": "r1"}})
    )
    assert d["ok"] is True
    assert "la clave funciona" in _paso(d, "lectura")["detalle"]


def test_un_config_sin_rutinas_declaradas_es_un_fallo(monkeypatch):
    """Si no hay identificadores, el lunes no hay nada que reescribir."""
    _con_hevy(monkeypatch, _HevyFalso({}))
    d = dg.probar_hevy(object(), _CfgFalso({}))
    assert d["ok"] is False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


# EL DOBLE DEVUELVE UN `SendResult` DE VERDAD, Y NO ES UN DETALLE
# ----------------------------------------------------------------
# Aquí había tres dobles que devolvían `type("R", (), {"ok": ..., "partes": ...})()`.
# `SendResult` no tiene ni `ok` ni `partes`: tiene `sent`, `parts`, `reason`,
# `error`, `preview` y `plain_parts`. Los tres tests pasaban -contra el doble- y
# el de «un cliente que dice que no envió no se pinta verde» daba por probada
# una rama que contra el cliente REAL era inalcanzable, porque el código leía
# `getattr(envio, "ok", True)` y el valor por defecto era `True`.
#
# O sea que el test verde estaba certificando justo lo contrario de lo que
# pasaba: con `send_enabled` en false, el botón decía «Telegram: todo correcto».
#
# Por eso el doble de abajo importa `SendResult` en vez de inventarse una forma,
# y por eso lleva `bot_token` y `chat_id`: el `TelegramClient` real los tiene y
# el diagnóstico ahora los mira.


def _cli_falso(resultado, enviados=None, token="123:abc", chat="-100"):
    """Un doble con la forma del `TelegramClient` real, ni más ni menos."""

    @doble_de(TelegramClient)
    class Cli:
        bot_token = token
        chat_id = chat

        def send(self, text, *, dry_run=False):
            if enviados is not None:
                enviados.append(text)
            return resultado

    return Cli()


def _con_telegram(monkeypatch, cliente):
    import app.integrations.telegram as t

    monkeypatch.setattr(t, "build_client", lambda *a, **k: cliente)


def test_el_doble_de_telegram_tiene_la_forma_del_cliente_real():
    """El doble y el original, campo a campo.

    Si `SendResult` gana o pierde un campo, este test se pone rojo antes de que
    el resto de la batería empiece a certificar una forma que ya no existe. Es
    la comprobación que faltaba las cuatro veces que un doble ha mentido.
    """
    from dataclasses import fields

    from app.integrations.telegram import SendResult, TelegramClient

    assert {f.name for f in fields(SendResult)} == {
        "sent", "parts", "reason", "error", "preview", "plain_parts",
    }
    # Y el cliente: lo que el diagnóstico le lee tiene que existir de verdad.
    for atributo in ("bot_token", "chat_id", "send_enabled", "send"):
        assert hasattr(TelegramClient, atributo) or atributo in TelegramClient.__annotations__


def test_telegram_manda_un_mensaje_de_verdad(monkeypatch):
    """Aquí no hay modo seco que valga: el mensaje ES la prueba.

    `dry_run` comprueba que el texto se puede montar, que es lo que ya comprueba
    el runner todas las mañanas. Lo que este botón tiene que demostrar es que
    LLEGA.
    """
    from app.integrations.telegram import SendResult

    enviados = []
    _con_telegram(monkeypatch, _cli_falso(SendResult(sent=True, parts=1), enviados))
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is True
    assert enviados == [dg.TEXTO_POR_DEFECTO]


def test_el_texto_propio_sustituye_al_de_por_defecto(monkeypatch):
    from app.integrations.telegram import SendResult

    enviados = []
    _con_telegram(monkeypatch, _cli_falso(SendResult(sent=True, parts=1), enviados))
    dg.probar_telegram(object(), None, texto="hola")
    assert enviados == ["hola"]


def test_con_los_envios_apagados_en_el_yaml_el_boton_no_se_pinta_verde(monkeypatch):
    """El caso que el botón lleva desde siempre certificando al revés.

    `integrations.telegram.send_enabled: false` devuelve `sent=False` con el
    motivo escrito. El mensaje NO sale, y un diagnóstico que lo llama correcto
    hace descartar Telegram como causa justo cuando Telegram es la causa.
    """
    from app.integrations.telegram import SendResult

    _con_telegram(monkeypatch, _cli_falso(SendResult(
        sent=False, parts=1,
        reason="integrations.telegram.send_enabled está en false",
    )))
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is False
    assert "send_enabled" in _paso(d, "envío")["detalle"]


def test_un_error_http_de_telegram_no_se_pinta_verde(monkeypatch):
    from app.integrations.telegram import SendResult

    _con_telegram(monkeypatch, _cli_falso(SendResult(
        sent=False, parts=0, error="parte 1/1 devolvió 401: Unauthorized",
    )))
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is False
    assert "401" in (_paso(d, "envío")["error"] or "")


def test_un_mensaje_que_salio_a_medias_no_es_un_envio_correcto(monkeypatch):
    """`sent=True` con `error` puesto: llegó una parte de tres.

    `send` devuelve `sent=enviados > 0`, así que el booleano solo no basta. Un
    mensaje de la mañana truncado se lee entero creyendo que estaba entero.
    """
    from app.integrations.telegram import SendResult

    _con_telegram(monkeypatch, _cli_falso(SendResult(
        sent=True, parts=1, error="parte 2/3 devolvió 500: Internal Server Error",
    )))
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is False
    assert "incompleto" in _paso(d, "envío")["detalle"]


def test_una_parte_sin_formato_se_cuenta_pero_no_es_un_fallo(monkeypatch):
    """Telegram rechazó el HTML y la parte salió en plano. Llegó, pero se dice."""
    from app.integrations.telegram import SendResult

    _con_telegram(monkeypatch, _cli_falso(SendResult(sent=True, parts=2, plain_parts=1)))
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is True
    assert "SIN formato" in _paso(d, "envío")["detalle"]


def test_sin_token_no_se_intenta_el_envio(monkeypatch):
    """`build_client` monta el cliente con el token vacío sin protestar.

    Decir «token y chat configurados» sin mirarlos era afirmar algo que el paso
    no había comprobado: con el token vacío la petición se va a
    `api.telegram.org/bot/sendMessage` y el fallo aparece un eslabón más allá,
    contado como si fuera de la red.
    """
    from app.integrations.telegram import SendResult

    _con_telegram(monkeypatch, _cli_falso(SendResult(sent=True, parts=1), token="  "))
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is False
    assert _paso(d, "credenciales")["ok"] is False
    assert _paso(d, "envío")["ok"] is None


def test_sin_chat_no_se_intenta_el_envio(monkeypatch):
    from app.integrations.telegram import SendResult

    _con_telegram(monkeypatch, _cli_falso(SendResult(sent=True, parts=1), chat=""))
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is False
    assert "chat" in _paso(d, "credenciales")["detalle"]


def test_sin_cliente_de_telegram_el_envio_no_se_intenta(monkeypatch):
    import app.integrations.telegram as t

    def revienta(*a, **k):
        raise RuntimeError("falta el token")

    monkeypatch.setattr(t, "build_client", revienta)
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is False
    assert _paso(d, "envío")["ok"] is None
    assert "no se intentó" in _paso(d, "envío")["detalle"]
