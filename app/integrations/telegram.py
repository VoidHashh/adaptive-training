"""Envío de mensajes a Telegram. Solo sabe enviar cadenas.

El texto lo compone `engine/message.py`, que es una función pura. Esa
separación es la que permite que `--dry-run` imprima el mensaje EXACTO que se
enviaría sin tocar la red.

Este módulo hace tres cosas y ninguna más: parte el mensaje si excede el límite
de Telegram, lo envía, y devuelve qué pasó. No decide contenido, no reintenta
indefinidamente y no traga errores.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

LIMIT = 4096
TIMEOUT_S = 20.0

# Las etiquetas que este proyecto usa de verdad. Telegram acepta unas cuantas
# más, pero enumerar las que usamos es lo que permite quitarlas sin adivinar.
ETIQUETAS = ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>")


class TelegramError(RuntimeError):
    """Fallo enviando a Telegram."""


def escapar_html(texto: Any) -> str:
    """Los tres caracteres que rompen el `parse_mode=HTML` de Telegram.

    TODO lo que se interpole dentro del mensaje y no sea una etiqueta nuestra
    tiene que pasar por aquí: nombres de ejercicio del `config.yaml`, títulos que
    vienen de Hevy y, sobre todo, el texto de una excepción.

    Esto no es higiene preventiva, es un fallo que estaba puesto. `scheduler.py`
    metía `str(excepción)` dentro de `<code>...</code>` sin tocar, y los `str` de
    las excepciones de Python llevan ángulos constantemente: basta un
    `TypeError: '<' not supported between instances of...` para que Telegram
    conteste `400 Bad Request: can't parse entities` y el aviso no salga. O sea
    que el único efecto hacia fuera que no tiene freno -el que avisa de que un
    trabajo ha reventado- se caía justo por reventar el trabajo.

    Comprobado contra la API de verdad, no contra un doble: el 400 llega ANTES
    incluso de que Telegram mire si el chat existe.

    El orden importa: `&` primero, o se re-escaparían los `&` que acabamos de
    escribir nosotros.
    """
    return (
        str(texto)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def sin_etiquetas(texto: str) -> str:
    """El mismo texto en plano: sin etiquetas y con los ángulos de vuelta.

    Se usa en dos sitios y por el mismo motivo: cuando el mensaje va a salir
    SIN `parse_mode`, dejar `<b>` o `&lt;` a la vista sería peor que no
    formatear nada.
    """
    for t in ETIQUETAS:
        texto = texto.replace(t, "")
    return (
        texto.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )


@dataclass
class SendResult:
    sent: bool
    parts: int = 0
    reason: str = ""
    error: str | None = None
    preview: list[str] = field(default_factory=list)
    # Partes que salieron en plano porque Telegram rechazó el HTML. Es un
    # número y no un booleano porque un mensaje largo puede ir a trozos y
    # rechazarse solo uno.
    plain_parts: int = 0


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Parte por saltos de línea, nunca a media palabra.

    `message.py` ya recorta a 4096, así que en la práctica esto casi nunca se
    usa. Está porque "casi nunca" no es "nunca": el día que un mensaje se pase,
    es preferible que llegue en dos trozos a que Telegram lo rechace entero y
    esa mañana no llegue nada.
    """
    if len(text) <= limit:
        return [text]

    partes: list[str] = []
    actual: list[str] = []
    largo = 0
    for linea in text.split("\n"):
        # +1 por el salto de línea que se volverá a añadir al unir.
        if largo + len(linea) + 1 > limit and actual:
            partes.append("\n".join(actual))
            actual, largo = [], 0
        actual.append(linea)
        largo += len(linea) + 1
    if actual:
        partes.append("\n".join(actual))
    return partes


def _rechaza_las_entidades(respuesta: Any) -> bool:
    """¿Es este 400 el de «no sé leer tu HTML»?

    Se mira el texto de la respuesta porque Telegram no da otra cosa: el código
    es 400 para esto y para media docena de motivos más. La frase exacta que
    devuelve, comprobada contra la API de verdad, es

        Bad Request: can't parse entities: Unsupported start tag "'" at byte 54

    Se acota a `parse entities` a propósito. Reintentar en plano un 400 de
    «chat not found» o de «bot was blocked» no arregla nada y además escondería
    el motivo de verdad detrás de un segundo error idéntico.
    """
    if getattr(respuesta, "status_code", None) != 400:
        return False
    return "parse entities" in (getattr(respuesta, "text", "") or "").lower()


@dataclass
class TelegramClient:
    bot_token: str
    chat_id: str
    send_enabled: bool = True

    def send(self, text: str, *, dry_run: bool = False) -> SendResult:
        partes = split_message(text)

        if dry_run:
            return SendResult(
                sent=False,
                parts=len(partes),
                reason="--dry-run: no se envía",
                preview=partes,
            )

        if not self.send_enabled:
            return SendResult(
                sent=False,
                parts=len(partes),
                reason="integrations.telegram.send_enabled está en false",
                preview=partes,
            )

        if not self.bot_token or not self.chat_id:
            raise TelegramError(
                "Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID en el .env"
            )

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise TelegramError("falta el paquete `httpx`") from exc

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        enviados = 0
        en_plano = 0
        with httpx.Client(timeout=TIMEOUT_S) as c:
            for i, parte in enumerate(partes, 1):
                r = c.post(url, json=self._cuerpo(parte))

                if _rechaza_las_entidades(r):
                    # La red de seguridad, y la razón de que exista: un mensaje
                    # con el HTML mal formado se pierde ENTERO, y las veces que
                    # eso puede pasar son justo las malas -el texto de una
                    # excepción, el motivo de un fallo de Hevy-. Es decir, el
                    # mensaje se caería exactamente el día que hace falta.
                    #
                    # Así que se reintenta en plano. Un aviso feo que llega vale
                    # infinitamente más que uno bonito que no. El escapado de
                    # `escapar_html` debería hacer que esto no salte nunca; está
                    # para lo que se me haya escapado, que es el motivo por el
                    # que el proyecto tiene esta avería en primer lugar.
                    log.error(
                        "Telegram rechazó el HTML de la parte %d/%d (%s). "
                        "Se reintenta en plano.",
                        i, len(partes), r.text[:200],
                    )
                    r = c.post(url, json=self._cuerpo(sin_etiquetas(parte), html=False))
                    if r.status_code == 200:
                        enviados += 1
                        en_plano += 1
                        continue

                if r.status_code != 200:
                    # Se dice cuántos trozos SÍ salieron: si el mensaje llegó a
                    # medias, quien lo lea por la mañana tiene que poder saber
                    # que lo que ve está incompleto.
                    return SendResult(
                        sent=enviados > 0,
                        parts=enviados,
                        plain_parts=en_plano,
                        error=(
                            f"parte {i}/{len(partes)} devolvió {r.status_code}: "
                            f"{r.text[:200]}"
                        ),
                    )
                enviados += 1

        return SendResult(
            sent=True,
            parts=enviados,
            plain_parts=en_plano,
            reason=(
                "enviado"
                if not en_plano
                else f"enviado, {en_plano} parte(s) SIN formato: Telegram "
                     f"rechazó el HTML (mira el log)"
            ),
        )

    def _cuerpo(self, texto: str, *, html: bool = True) -> dict[str, Any]:
        cuerpo: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": texto,
            "disable_web_page_preview": True,
        }
        if html:
            cuerpo["parse_mode"] = "HTML"
        return cuerpo


def build_client(settings: Any, config: Any = None) -> TelegramClient:
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    tg_cfg = ((raw.get("integrations") or {}).get("telegram") or {})
    return TelegramClient(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        send_enabled=bool(tg_cfg.get("send_enabled", True)),
    )
