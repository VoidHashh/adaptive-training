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
    "load_3d": 100.0,
    "weekend_intense_rides": 0,
    "weekend_total_hours": 1.0,
}

# Los percentiles NO son señales: viven en `Signals.adaptive` y las reglas los
# leen por ahí (`gt_adaptive: load_3d_p90`), no en `values`. Ponerlos en
# `values` no da error, simplemente no los encuentra nadie.
UMBRALES_COMPLETOS: dict[str, float] = {
    "load_3d_p90": 200.0,
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
