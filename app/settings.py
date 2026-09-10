"""Configuración de entorno (secretos y rutas).

Todo lo que sea un secreto vive aquí y viene del entorno / .env.
Todo lo que sea una regla de entrenamiento vive en config.yaml.
Esa separación es deliberada: config.yaml se puede versionar en git,
.env no (está en .gitignore).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
