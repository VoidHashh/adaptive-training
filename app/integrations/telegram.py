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


class TelegramError(RuntimeError):
    """Fallo enviando a Telegram."""


@dataclass
class SendResult:
    sent: bool
    parts: int = 0
    reason: str = ""
    error: str | None = None
    preview: list[str] = field(default_factory=list)


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
        with httpx.Client(timeout=TIMEOUT_S) as c:
            for i, parte in enumerate(partes, 1):
                r = c.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": parte,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if r.status_code != 200:
                    # Se dice cuántos trozos SÍ salieron: si el mensaje llegó a
                    # medias, quien lo lea por la mañana tiene que poder saber
                    # que lo que ve está incompleto.
                    return SendResult(
                        sent=enviados > 0,
                        parts=enviados,
                        error=(
                            f"parte {i}/{len(partes)} devolvió {r.status_code}: "
                            f"{r.text[:200]}"
                        ),
                    )
                enviados += 1

        return SendResult(sent=True, parts=enviados, reason="enviado")


def build_client(settings: Any, config: Any = None) -> TelegramClient:
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    tg_cfg = ((raw.get("integrations") or {}).get("telegram") or {})
    return TelegramClient(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        send_enabled=bool(tg_cfg.get("send_enabled", True)),
    )
