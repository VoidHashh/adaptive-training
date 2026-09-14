"""Envío a Telegram.

El contenido del mensaje lo compone `engine/message.py` y se prueba allí. Aquí
solo importa que el transporte no mienta: que `--dry-run` no toque la red, que
el interruptor se respete, y que un mensaje entregado a medias se pueda
detectar como tal.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from app.integrations import telegram
from app.integrations.telegram import (
    LIMIT,
    TelegramClient,
    TelegramError,
    escapar_html,
    sin_etiquetas,
    split_message,
)

from tests.conftest import FakeHTTP, FakeResponse, _telegram_rechazaria
from tests.dobles import doble_de, no_es_doble
from app.settings import Settings


def _httpx_falso(doble: FakeHTTP):
    """Un módulo con `.Client(...)` que devuelve siempre el mismo doble.

    `telegram.send` hace `import httpx` DENTRO de la función, así que no basta
    con sustituir un atributo del módulo: hay que sustituir la entrada de
    `sys.modules`, que es lo que resuelve ese import.
    """

    @no_es_doble("suplanta al MODULO httpx, no a una clase de app/")
    class ModuloFalso:
        @staticmethod
        def Client(*args, **kwargs):  # noqa: N802 - imita la API de httpx
            return doble

    return ModuloFalso


@pytest.fixture
def red(monkeypatch):
    """Devuelve una función que prepara respuestas y entrega el doble."""

    def preparar(*respuestas) -> FakeHTTP:
        doble = FakeHTTP(list(respuestas))
        monkeypatch.setitem(sys.modules, "httpx", _httpx_falso(doble))
        return doble

    return preparar


def cliente(**kwargs) -> TelegramClient:
    return TelegramClient(bot_token="123:abc", chat_id="42", **kwargs)


# ---------------------------------------------------------------------------
# Partido del mensaje
# ---------------------------------------------------------------------------


def test_un_mensaje_corto_no_se_parte():
    assert split_message("hola") == ["hola"]


def test_un_mensaje_en_el_limite_exacto_no_se_parte():
    texto = "x" * LIMIT
    assert split_message(texto) == [texto]


def test_se_parte_por_saltos_de_linea_nunca_a_media_palabra():
    lineas = [f"linea {i} con algo de texto para ocupar" for i in range(400)]
    partes = split_message("\n".join(lineas))
    assert len(partes) > 1
    for p in partes:
        assert len(p) <= LIMIT
    recompuesto = "\n".join(partes)
    for linea in lineas:
        assert linea in recompuesto


def test_al_partir_no_se_pierde_ni_se_duplica_contenido():
    lineas = [f"{i:04d}" for i in range(2000)]
    partes = split_message("\n".join(lineas))
    assert "\n".join(partes).split("\n") == lineas


# ---------------------------------------------------------------------------
# Envío
# ---------------------------------------------------------------------------


def test_el_dry_run_no_toca_la_red_y_devuelve_el_texto_exacto(red):
    """`--dry-run` tiene que enseñar el mensaje EXACTO que se enviaría."""
    doble = red()  # sin respuestas: cualquier petición reventaría el test
    r = cliente().send("hola", dry_run=True)
    assert not r.sent
    assert r.preview == ["hola"]
    assert "--dry-run" in r.reason
    assert doble.llamadas == []


def test_el_interruptor_apagado_no_envia(red):
    doble = red()
    r = cliente(send_enabled=False).send("hola")
    assert not r.sent
    assert "send_enabled" in r.reason
    assert r.preview == ["hola"]
    assert doble.llamadas == []


def test_sin_credenciales_se_lanza_en_vez_de_callar():
    c = TelegramClient(bot_token="", chat_id="42")
    with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
        c.send("hola")


def test_un_envio_correcto_reporta_las_partes(red):
    doble = red(FakeResponse(200, {"ok": True}))
    r = cliente().send("hola")
    assert r.sent
    assert r.parts == 1
    assert doble.llamadas[0]["json"]["chat_id"] == "42"
    assert doble.llamadas[0]["json"]["text"] == "hola"
    assert doble.llamadas[0]["json"]["parse_mode"] == "HTML"
    assert "/bot123:abc/sendMessage" in doble.llamadas[0]["url"]


def test_un_mensaje_entregado_a_medias_se_declara_a_medias(red):
    """Quien lo lea por la mañana tiene que poder saber que está incompleto."""
    red(FakeResponse(200, {"ok": True}), FakeResponse(429, text="calma"))
    largo = "\n".join(f"linea {i} " + "x" * 60 for i in range(120))
    total = len(split_message(largo))
    assert total >= 2, "el texto del test ya no se parte"

    r = cliente().send(largo)
    assert r.sent is True, "algo llegó"
    assert r.parts == 1, "pero solo la primera parte"
    assert "429" in (r.error or "")
    assert f"parte 2/{total}" in r.error, "hay que decir cuál falló y de cuántas"


def test_si_falla_la_primera_parte_no_se_dice_que_se_envio(red):
    red(FakeResponse(500, text="boom"))
    r = cliente().send("hola")
    assert not r.sent
    assert r.parts == 0
    assert "500" in (r.error or "")


# ---------------------------------------------------------------------------
# El HTML, que es lo que tumbaba el aviso
# ---------------------------------------------------------------------------
#
# El doble de `conftest` ya no acepta cualquier texto: reproduce el 400 de
# `can't parse entities` medido contra la API real. Eso convierte a TODOS los
# tests de arriba en tests del contrato, no solo a los de aquí abajo.


@pytest.mark.parametrize(
    "roto",
    [
        "TypeError: '<' not supported between instances of 'str' and 'int'",
        "5 < 7",
        "hola <foo>mundo</foo>",
        "<b>sin cerrar",
        "cierre </b> suelto",
        "<b>a<i>b</b>c</i>",
    ],
)
def test_el_doble_rechaza_el_html_que_rechaza_telegram(roto):
    """La falsación del propio doble: si esto pasara, no probaría nada.

    Son los seis textos que se mandaron a la API de verdad con un `chat_id`
    inválido -para que no le llegara a nadie- y que contestaron con un 400 de
    entidades antes siquiera de mirar el chat.
    """
    r = _telegram_rechazaria({"text": roto, "parse_mode": "HTML"})
    assert r is not None and r.status_code == 400
    assert "can't parse entities" in r.text


@pytest.mark.parametrize("bueno", ["Tom & Jerry", "algo &fo; mas", "3 > 2", "3->4"])
def test_el_doble_no_inventa_rechazos_que_telegram_no_hace(bueno):
    """Y la mitad que más se olvida: lo que la API SÍ acepta.

    Un doble que rechazara de más también mentiría, y encima haría escribir
    código para contentarlo. El `&` suelto Telegram lo perdona; medido.
    """
    assert _telegram_rechazaria({"text": bueno, "parse_mode": "HTML"}) is None


def test_el_texto_de_una_excepcion_escapado_ya_no_lo_rechaza():
    """El fallo concreto, reducido a una línea.

    `scheduler.py` metía esto tal cual dentro de `<code>`. El `<` del mensaje
    de la excepción abría una etiqueta inventada y el aviso entero se perdía.
    """
    exc = "TypeError: '<' not supported between instances of 'str' and 'int'"
    crudo = f"<code>{exc}</code>"
    escapado = f"<code>{escapar_html(exc)}</code>"

    assert _telegram_rechazaria({"text": crudo, "parse_mode": "HTML"}) is not None
    assert _telegram_rechazaria({"text": escapado, "parse_mode": "HTML"}) is None


def test_si_telegram_rechaza_el_html_el_mensaje_sale_en_plano(red):
    """La red de seguridad. Un aviso feo que llega vale más que uno que no.

    El escapado debería hacer que esto no salte nunca. Está para lo que se haya
    escapado, que es el motivo por el que el proyecto tuvo esta avería.
    """
    doble = red(FakeResponse(200, {"ok": True}))  # solo para el reintento
    r = cliente().send("<b>roto: 5 < 7</b>")

    assert r.sent is True, "el aviso tiene que salir igual"
    assert r.parts == 1
    assert r.plain_parts == 1, "y tiene que constar que salió degradado"
    assert "SIN formato" in r.reason

    assert len(doble.llamadas) == 2, "un intento en HTML y el reintento en plano"
    assert doble.llamadas[0]["json"]["parse_mode"] == "HTML"
    assert "parse_mode" not in doble.llamadas[1]["json"]
    assert doble.llamadas[1]["json"]["text"] == "roto: 5 < 7", (
        "en plano se quitan las etiquetas Y se deshace el escapado"
    )


def test_un_400_que_no_es_de_entidades_no_se_reintenta(red):
    """`chat not found` no se arregla mandándolo otra vez sin formato.

    Reintentarlo escondería el motivo de verdad detrás de un segundo error
    idéntico, y encima diría que se envió.
    """
    doble = red(FakeResponse(400, text="Bad Request: chat not found"))
    r = cliente().send("hola")

    assert not r.sent
    assert r.plain_parts == 0
    assert "chat not found" in (r.error or "")
    assert len(doble.llamadas) == 1, "no se reintenta"


def test_si_el_reintento_en_plano_tambien_falla_no_se_dice_que_se_envio(red):
    red(FakeResponse(500, text="boom"))
    r = cliente().send("<b>roto: 5 < 7</b>")
    assert not r.sent
    assert r.parts == 0
    assert "500" in (r.error or "")


def test_escapar_html_hace_el_ampersand_primero():
    """Al revés se re-escaparían los `&` que acabamos de escribir nosotros."""
    assert escapar_html("<a & b>") == "&lt;a &amp; b&gt;"


def test_sin_etiquetas_deshace_lo_que_escapar_html_hizo():
    """Ida y vuelta: lo que sale por consola es el texto original."""
    original = "Tom & Jerry: 5 < 7 > 3"
    assert sin_etiquetas(f"<b>{escapar_html(original)}</b>") == original


def test_sin_etiquetas_conoce_code_que_la_lista_vieja_no_conocia():
    """La lista propia de `render_plain` solo tenía `<b>` e `<i>`."""
    assert sin_etiquetas("<code>x</code>") == "x"


# ---------------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------------


@doble_de(Settings)
@dataclass
class SettingsFalsos:
    telegram_bot_token: str = "123:abc"
    telegram_chat_id: str = "42"


def test_build_client_lee_el_interruptor_del_yaml(cfg):
    c = telegram.build_client(SettingsFalsos(), cfg)
    assert c.send_enabled is bool(
        cfg.raw["integrations"]["telegram"].get("send_enabled", True)
    )


def test_build_client_envia_por_defecto_si_falta_la_seccion():
    """Leer no rompe nada: el valor por defecto de Telegram sí es 'enviar'.

    Es la asimetría deliberada con Hevy, cuyo interruptor arranca apagado
    porque su PUT es destructivo.
    """
    assert telegram.build_client(SettingsFalsos(), {}).send_enabled is True
