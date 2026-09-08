"""Configuración de entorno (secretos y rutas).

Todo lo que sea un secreto vive aquí y viene del entorno / .env.
Todo lo que sea una regla de entrenamiento vive en config.yaml.
Esa separación es deliberada: config.yaml se puede versionar en git,
.env no (está en .gitignore).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
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
    log_level: str = "INFO"

    # Si es true, el sistema decide y registra pero NO escribe en Hevy ni
    # envía Telegram. Equivale al flag --dry-run de la CLI.
    dry_run: bool = Field(default=False)

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
