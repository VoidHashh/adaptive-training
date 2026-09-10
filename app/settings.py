"""Configuración de entorno (secretos y rutas).

Todo lo que sea un secreto vive aquí y viene del entorno / .env.
Todo lo que sea una regla de entrenamiento vive en config.yaml.
Esa separación es deliberada: config.yaml se puede versionar en git,
.env no (está en .gitignore).
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent

# Prefijos que son nuestros y de nadie más. Una variable que empiece por uno de
# estos y no corresponda a ningún campo es una errata, no la variable de otro
# programa: en este contenedor no vive nada más.
PREFIJOS_PROPIOS = ("GARMIN_", "HEVY_", "TELEGRAM_")

# Umbral de parecido para el resto. Medido contra el entorno real de Windows y
# contra las variables que trae la imagen base de Python (PATH, HOME, HOSTNAME,
# LANG, GPG_KEY, PYTHON_VERSION, PYTHONUNBUFFERED, VIRTUAL_ENV...): ninguna
# llega a 0.85, y sí lo pasan DRY_RUM, DRYRUN, LOGLEVEL, CONFIGPATH o
# DATABASE_URI, que son las erratas que de verdad se cometen.
PARECIDO_MINIMO = 0.85


def erratas_de_entorno(entorno: dict[str, str], campos: set[str]) -> list[tuple[str, str]]:
    """Variables de entorno que se parecen a un ajuste nuestro y no lo son.

    Existe por el hueco que deja `extra="forbid"`. Forbid protege el FICHERO
    `.env`, y eso cubre la CLI y el desarrollo local, pero dentro de Docker no
    hay `.env`: está en `.dockerignore` a propósito, y los valores entran como
    variables de entorno vía `env_file:` de compose. Ahí forbid no mira nada,
    porque pydantic solo lee del entorno las variables que ya conoce.

    Es decir: justo en producción, que es donde el sistema decide solo, un
    `DRY_RUM=true` se ignoraba en silencio y `dry_run` se quedaba en `False`.
    Se habría escrito en Hevy y enviado Telegram el día en que se pidió
    expresamente que no.

    Devuelve pares (lo_que_hay, lo_que_seguramente_se_quería).
    """
    fuera: list[tuple[str, str]] = []
    conocidos = sorted(c.upper() for c in campos)
    for nombre in sorted(entorno):
        n = nombre.upper()
        if n in conocidos:
            continue
        if n.startswith(PREFIJOS_PROPIOS):
            # Sin umbral: el prefijo ya es prueba suficiente. `get_close_matches`
            # se usa solo para sugerir el más parecido de los nuestros.
            sug = difflib.get_close_matches(n, conocidos, n=1, cutoff=0.0)
            fuera.append((nombre, sug[0] if sug else "—"))
            continue
        sug = difflib.get_close_matches(n, conocidos, n=1, cutoff=PARECIDO_MINIMO)
        if sug:
            fuera.append((nombre, sug[0]))
    return fuera


class Settings(BaseSettings):
    # `forbid` y no `ignore`: con `ignore`, una errata en el `.env` no era un
    # error, era un ajuste que no existía. `DRY_RUM=true` se leía, se
    # descartaba, y el arranque seguía tan contento con `dry_run=False`.
    #
    # Comprobado que es seguro en las tres formas en que esto arranca:
    #   1. `.env` limpio + variables de entorno ajenas (TZ, PATH, HOSTNAME...)
    #      -> arranca; pydantic solo mira del entorno los campos declarados.
    #   2. Errata dentro del `.env` -> lanza, que es de lo que se trata.
    #   3. Sin `.env`, todo por entorno (el caso Docker) -> arranca.
    # El agujero del caso 3 lo tapa `erratas_de_entorno`, más abajo.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # --- Garmin -------------------------------------------------------------
    garmin_email: str = ""
    garmin_password: str = ""
    # Directorio persistente de tokens. En Docker va al volumen, para no
    # tener que volver a hacer login (y a comerse los 429) en cada arranque.
    garmin_token_dir: Path = REPO_ROOT / "data" / "garmin_tokens"

    # --- Hevy ---------------------------------------------------------------
    hevy_api_key: str = ""
    hevy_api_base: str = "https://api.hevyapp.com"

    # --- Telegram -----------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Aplicación ---------------------------------------------------------
    config_path: Path = REPO_ROOT / "config.yaml"
    database_url: str = f"sqlite:///{(REPO_ROOT / 'data' / 'app.db').as_posix()}"

    # Se APLICA en el arranque de la API (`app.api.lifespan`). Antes solo lo
    # aplicaba la CLI, así que dentro de Docker -que es donde vive esto- la
    # variable no hacía absolutamente nada: el logger raíz se quedaba en WARNING
    # y todos los `log.info` del sistema iban a la basura. Poner `LOG_LEVEL=DEBUG`
    # para averiguar por qué el motor decidió lo que decidió no producía ni una
    # línea, y `docker compose logs` -que es lo que el README manda mirar cuando
    # algo va mal- estaba vacío por construcción.
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _nivel_valido(cls, v: str) -> str:
        """Un nivel mal escrito es un error, no un WARNING silencioso.

        `logging.basicConfig(level="INFORMATION")` lanza; peor sería tragárselo y
        caer a WARNING, porque el usuario habría pedido DEBUG, no lo vería, y
        concluiría que el motor no tiene nada que contar.
        """
        nivel = str(v).strip().upper()
        validos = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if nivel not in validos:
            raise ValueError(
                f"LOG_LEVEL={v!r} no es un nivel de log. Usa uno de: "
                f"{', '.join(sorted(validos))}."
            )
        return nivel

    # Si es true, el sistema decide y registra pero NO escribe en Hevy ni
    # envía Telegram. Equivale al flag --dry-run de la CLI.
    dry_run: bool = Field(default=False)

    # Los tres trabajos del día (Garmin 06:30, decisión 09:00, reconciliar
    # 22:30). Por defecto SÍ, porque un sistema que decide solo y no tiene
    # planificador no decide nada: sirve la PWA, contesta "ok" en /api/health y
    # no pasa nada nunca. Solo se apaga en los tests y en un eventual segundo
    # proceso que sirva la web sin duplicar los trabajos -dos planificadores
    # sobre la misma base son dos decisiones pisándose el mismo día-.
    scheduler_enabled: bool = Field(default=True)

    def __init__(self, **kw):
        super().__init__(**kw)
        erratas = erratas_de_entorno(dict(os.environ), set(type(self).model_fields))
        if erratas:
            detalle = "; ".join(f"{mal} (¿querías {bien}?)" for mal, bien in erratas)
            raise ValueError(
                f"variable(s) de entorno que no son ningún ajuste: {detalle}. "
                f"Un ajuste mal escrito no se aplica y no avisa: el sistema "
                f"arranca con el valor por defecto y decide como si nunca lo "
                f"hubieras puesto. Corrígelo o quítalo del entorno."
            )

    # --- Ayudas -------------------------------------------------------------
    def missing_secrets(self) -> list[str]:
        """Devuelve los secretos que hacen falta y no están puestos.

        No lanza excepción: el sistema debe poder arrancar a medio configurar
        (por ejemplo para rellenar el formulario) y avisar de qué falta, en vez
        de negarse a arrancar.
        """
        required = {
            "GARMIN_EMAIL": self.garmin_email,
            "GARMIN_PASSWORD": self.garmin_password,
            "HEVY_API_KEY": self.hevy_api_key,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
        }
        return [name for name, value in required.items() if not value]


settings = Settings()
