"""Utilidades compartidas por la batería de pruebas.

Dos decisiones de fondo:

1. **Se prueba contra el `config.yaml` real, no contra uno de juguete.** Un
   config de prueba pasaría los tests el día que el de verdad se rompa, que es
   exactamente el día en el que hacen falta. Cuando un test necesita variar algo
   se hace una copia profunda (`cfg_copia`) y se toca ahí.

2. **Ningún test toca la red ni el disco del usuario.** Lo que necesita disco
   usa `tmp_path`; lo que necesita HTTP usa el doble de `FakeHTTP`. Una batería
   que depende de que Garmin conteste no es una batería, es una apuesta.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

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


@pytest.fixture
def cfg_summer(cfg):
    """Variante de calendario que sí entrena las tres rutinas.

    La activa (`with_pool`) no programa `dia_3` ningún día, así que los tests
    que necesitan esa rutina tienen que cambiar de variante explícitamente en
    vez de dar por hecho que existe en el calendario.
    """
    c = copy.deepcopy(cfg)
    c.raw["calendar"]["active_variant"] = "summer"
    return c


# ---------------------------------------------------------------------------
# Constructores de datos sintéticos
# ---------------------------------------------------------------------------


def sig(day: date, history: dict[str, dict[date, Any]] | None = None, **values) -> Signals:
    """Un `Signals` a mano, sin pasar por `build_signals`."""
    return Signals(day=day, values=values, history=history or {})


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


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self) -> Any:
        return self._payload


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
        return self._siguiente("put", url, **kwargs)

    def post(self, url: str, **kwargs) -> FakeResponse:
        return self._siguiente("post", url, **kwargs)
