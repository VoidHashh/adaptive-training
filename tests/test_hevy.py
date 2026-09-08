"""Escritura en Hevy: el módulo más peligroso del proyecto.

`PUT /v1/routines/{id}` REEMPLAZA la rutina entera. Una escritura mal
construida no la degrada: la sustituye. Por eso aquí no se prueba solo que el
camino feliz funcione, sino sobre todo que los frenos muerdan:

  - el interruptor general corta antes de tocar la red,
  - sin copia verificada no se escribe,
  - una escritura interrumpida deja rastro,
  - y el `superset_id` sobrevive al viaje de ida y vuelta.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.integrations import hevy
from app.integrations.hevy import (
    Backup,
    HevyClient,
    HevyError,
    build_routine_payload,
    latest_backup,
    payload_diff,
    pending_marker,
    read_pending,
    save_backup,
)

from tests.conftest import FakeHTTP, FakeResponse


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


@dataclass
class SesionFalsa:
    """Lo mínimo de `BuiltSession` que mira `build_routine_payload`."""

    routine_key: str = "dia_1"
    title: str | None = "Día 1"
    notes: str | None = None
    exercises: list[dict[str, Any]] = field(default_factory=list)


def ejercicio(**kwargs) -> dict[str, Any]:
    base = {
        "key": "hip_thrust_barra",
        "name": "Hip thrust con barra",
        "template_id": "AAAA1111",
        "rest_seconds": 90,
        "sets": [
            {"type": "warmup", "reps": 10, "weight_kg": 20},
            {"type": "normal", "reps": 8, "weight_kg": 60},
        ],
    }
    base.update(kwargs)
    return base


def cliente(tmp_path: Path, respuestas=None, write_enabled=True) -> tuple[HevyClient, FakeHTTP]:
    doble = FakeHTTP(respuestas or [])
    c = HevyClient(
        api_key="clave-de-prueba",
        data_root=tmp_path,
        write_enabled=write_enabled,
    )
    c._client = lambda: doble  # type: ignore[method-assign]
    return c, doble


REMOTO = {
    "title": "Día 1",
    "notes": None,
    "exercises": [
        {
            "exercise_template_id": "AAAA1111",
            "title": "Hip thrust con barra",
            "sets": [
                {"type": "warmup", "reps": 10, "weight_kg": 20},
                {"type": "normal", "reps": 8, "weight_kg": 55},
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Construcción del cuerpo del PUT
# ---------------------------------------------------------------------------


def test_el_cuerpo_lleva_titulo_y_ejercicios_indexados():
    s = SesionFalsa(exercises=[ejercicio(), ejercicio(key="otro", template_id="BBBB2222")])
    body = build_routine_payload(s, {})
    assert body["routine"]["title"] == "Día 1"
    assert [e["index"] for e in body["routine"]["exercises"]] == [0, 1]
    assert [s_["index"] for s_ in body["routine"]["exercises"][0]["sets"]] == [0, 1]


def test_el_superset_id_se_copia_tal_cual():
    """Omitirlo deshace las superseries en la app, en silencio."""
    s = SesionFalsa(exercises=[ejercicio(superset_id=7)])
    body = build_routine_payload(s, {})
    assert body["routine"]["exercises"][0]["superset_id"] == 7


def test_sin_superset_va_none_explicito_no_ausente():
    s = SesionFalsa(exercises=[ejercicio()])
    ex = build_routine_payload(s, {})["routine"]["exercises"][0]
    assert "superset_id" in ex
    assert ex["superset_id"] is None


def test_los_campos_que_no_aplican_van_a_null_y_no_se_omiten():
    """Así el cuerpo tiene siempre la misma forma y el diff no miente."""
    s = SesionFalsa(exercises=[ejercicio()])
    serie = build_routine_payload(s, {})["routine"]["exercises"][0]["sets"][0]
    assert serie["distance_meters"] is None
    assert serie["duration_seconds"] is None
    assert serie["custom_metric"] is None


def test_el_tipo_de_serie_se_normaliza_a_minusculas():
    s = SesionFalsa(exercises=[ejercicio(sets=[{"type": "WARMUP", "reps": 10}])])
    serie = build_routine_payload(s, {})["routine"]["exercises"][0]["sets"][0]
    assert serie["type"] == "warmup"


def test_una_serie_sin_tipo_es_normal():
    s = SesionFalsa(exercises=[ejercicio(sets=[{"reps": 10}])])
    serie = build_routine_payload(s, {})["routine"]["exercises"][0]["sets"][0]
    assert serie["type"] == "normal"


def test_el_titulo_cae_a_la_definicion_del_config_si_la_sesion_no_lo_trae():
    s = SesionFalsa(title=None, exercises=[])
    cfg = {"routines": {"dia_1": {"title": "Día 1 (del YAML)"}}}
    assert build_routine_payload(s, cfg)["routine"]["title"] == "Día 1 (del YAML)"


def test_el_cuerpo_es_serializable_a_json():
    s = SesionFalsa(exercises=[ejercicio()])
    json.dumps(build_routine_payload(s, {}))


def test_el_cuerpo_construido_contra_el_config_real(cfg):
    """Comprobación de integración: la rutina real produce un cuerpo completo."""
    routine = cfg.raw["routines"]["dia_1"]
    s = SesionFalsa(
        routine_key="dia_1",
        title=routine.get("title"),
        exercises=[
            {**ex, "sets": ex["sets"]} for ex in routine["exercises"]
        ],
    )
    body = build_routine_payload(s, cfg)
    assert body["routine"]["exercises"]
    for ex in body["routine"]["exercises"]:
        assert ex["exercise_template_id"], "Hevy rechaza un ejercicio sin template_id"
        assert ex["sets"], "un ejercicio sin series no es un ejercicio"


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_el_diff_señala_el_cambio_de_peso():
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    lineas = payload_diff(REMOTO, nuevo)
    assert any("55 -> 60" in l for l in lineas)


def test_el_diff_señala_altas_y_bajas():
    nuevo = build_routine_payload(
        SesionFalsa(exercises=[ejercicio(key="nuevo", template_id="ZZZZ9999")]), {}
    )
    lineas = "\n".join(payload_diff(REMOTO, nuevo))
    assert "+ ALTA" in lineas
    assert "- BAJA" in lineas


def test_el_diff_señala_el_cambio_en_el_numero_de_series():
    nuevo = build_routine_payload(
        SesionFalsa(exercises=[ejercicio(sets=[{"type": "normal", "reps": 8, "weight_kg": 55}])]),
        {},
    )
    assert any("2 -> 1 series" in l for l in payload_diff(REMOTO, nuevo))


def test_sin_cambios_el_diff_lo_dice():
    igual = SesionFalsa(
        exercises=[
            ejercicio(
                sets=[
                    {"type": "warmup", "reps": 10, "weight_kg": 20},
                    {"type": "normal", "reps": 8, "weight_kg": 55},
                ]
            )
        ]
    )
    nuevo = build_routine_payload(igual, {})
    assert payload_diff(REMOTO, nuevo) == ["sin cambios respecto a lo que ya hay en Hevy"]


def test_sin_estado_remoto_el_diff_lo_admite_en_vez_de_fingir():
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    lineas = payload_diff(None, nuevo)
    assert "no se pudo leer el estado remoto" in lineas[0]


# ---------------------------------------------------------------------------
# Copias de seguridad
# ---------------------------------------------------------------------------


def test_la_copia_se_guarda_y_se_relee(tmp_path):
    b = save_backup(tmp_path, "r1", REMOTO)
    assert b.path.is_file()
    assert b.payload == REMOTO
    assert json.loads(b.path.read_text(encoding="utf-8"))["routine"] == REMOTO
    assert "1 ejercicios" in b.describe()


def test_una_copia_que_no_se_puede_releer_impide_escribir(tmp_path, monkeypatch):
    """La verificación no es paranoia decorativa.

    Descubrir que la copia no vale en el momento de revertir —que es siempre el
    peor momento— es exactamente lo que este módulo existe para evitar.
    """

    def escritura_corrupta(self, *args, **kwargs):  # noqa: ANN001
        return "{ esto no es json"

    monkeypatch.setattr(Path, "read_text", escritura_corrupta)
    with pytest.raises(HevyError, match="NO se escribe"):
        save_backup(tmp_path, "r1", REMOTO)


def test_una_copia_que_no_coincide_impide_escribir(tmp_path, monkeypatch):
    def otra_cosa(self, *args, **kwargs):  # noqa: ANN001
        return json.dumps({"routine_id": "r1", "taken_at": "2026-01-01T00:00:00",
                           "routine": {"title": "otra cosa"}})

    monkeypatch.setattr(Path, "read_text", otra_cosa)
    with pytest.raises(HevyError, match="no coincide"):
        save_backup(tmp_path, "r1", REMOTO)


def test_latest_backup_devuelve_la_mas_reciente(tmp_path):
    assert latest_backup(tmp_path, "r1") is None
    save_backup(tmp_path, "r1", REMOTO)
    otro = {**REMOTO, "title": "más nuevo"}
    save_backup(tmp_path, "r1", otro)
    ultimo = latest_backup(tmp_path, "r1")
    assert ultimo is not None
    assert ultimo.payload == otro


def test_una_copia_ilegible_no_revienta_la_lectura(tmp_path):
    save_backup(tmp_path, "r1", REMOTO)
    fichero = next((tmp_path / "hevy_backups" / "r1").glob("*.json"))
    fichero.write_text("{roto", encoding="utf-8")
    assert latest_backup(tmp_path, "r1") is None


# ---------------------------------------------------------------------------
# Marca de escritura en curso
# ---------------------------------------------------------------------------


def test_sin_marca_no_hay_escritura_pendiente(tmp_path):
    assert read_pending(tmp_path) is None


def test_una_marca_ilegible_se_reporta_igualmente(tmp_path):
    marca = pending_marker(tmp_path)
    marca.parent.mkdir(parents=True, exist_ok=True)
    marca.write_text("{roto", encoding="utf-8")
    pendiente = read_pending(tmp_path)
    assert pendiente is not None
    assert "ilegible" in pendiente["note"]


# ---------------------------------------------------------------------------
# write_routine
# ---------------------------------------------------------------------------


def test_el_interruptor_corta_antes_de_tocar_la_red(tmp_path):
    """Un interruptor que hay que acordarse de mirar no es un interruptor."""
    c, doble = cliente(tmp_path, write_enabled=False)
    r = c.write_routine("r1", {"routine": {}})
    assert not r.written
    assert "write_enabled" in r.reason
    assert doble.llamadas == [], "no debería haberse hecho ni la lectura previa"
    assert not (tmp_path / "hevy_backups").exists()


def test_el_dry_run_hace_la_copia_pero_no_envia_el_put(tmp_path):
    c, doble = cliente(tmp_path, [FakeResponse(200, REMOTO)])
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    r = c.write_routine("r1", nuevo, dry_run=True)
    assert not r.written
    assert r.backup is not None and r.backup.path.is_file()
    assert any("55 -> 60" in l for l in r.diff)
    assert [l["verb"] for l in doble.llamadas] == ["get"]
    assert read_pending(tmp_path) is None


def test_una_escritura_correcta_deja_copia_y_retira_la_marca(tmp_path):
    c, doble = cliente(tmp_path, [FakeResponse(200, REMOTO), FakeResponse(200, {"ok": True})])
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    r = c.write_routine("r1", nuevo)
    assert r.written
    assert r.backup is not None and r.backup.payload == REMOTO
    assert read_pending(tmp_path) is None, "la marca debe retirarse al confirmar"
    assert [l["verb"] for l in doble.llamadas] == ["get", "put"]
    assert doble.llamadas[1]["json"] == nuevo


def test_un_put_rechazado_no_borra_la_marca_ni_la_copia(tmp_path):
    c, _ = cliente(tmp_path, [FakeResponse(200, REMOTO), FakeResponse(400, text="mal")])
    r = c.write_routine("r1", {"routine": {"exercises": []}})
    assert not r.written
    assert "400" in (r.error or "")
    assert r.backup is not None and r.backup.path.is_file()


def test_si_la_red_falla_la_marca_se_queda_puesta(tmp_path):
    """No sabemos si el PUT llegó. Fingir que no llegó sería mentir."""
    doble = FakeHTTP([FakeResponse(200, REMOTO)])
    c = HevyClient(api_key="k", data_root=tmp_path, write_enabled=True)

    llamadas = {"n": 0}

    def cliente_que_revienta():
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return doble
        raise ConnectionError("se cayó la red a mitad")

    c._client = cliente_que_revienta  # type: ignore[method-assign]
    r = c.write_routine("r1", {"routine": {"exercises": []}})

    assert not r.written
    assert "NO se sabe si Hevy" in (r.error or "")
    pendiente = read_pending(tmp_path)
    assert pendiente is not None, "sin marca, mañana nadie sabría que hay una rutina dudosa"
    assert pendiente["routine_id"] == "r1"
    assert Path(pendiente["backup"]).is_file()


def test_un_get_que_falla_impide_la_escritura(tmp_path):
    c, _ = cliente(tmp_path, [FakeResponse(500, text="boom")])
    with pytest.raises(HevyError, match="500"):
        c.write_routine("r1", {"routine": {}})
    assert read_pending(tmp_path) is None


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restaurar_ignora_el_interruptor(tmp_path):
    """Si el modo seguro bloqueara la reversión, impediría deshacer un desastre
    causado mientras el interruptor estaba abierto."""
    save_backup(tmp_path, "r1", REMOTO)
    c, doble = cliente(tmp_path, [FakeResponse(200, {"ok": True})], write_enabled=False)
    r = c.restore("r1")
    assert r.written
    assert doble.llamadas[0]["verb"] == "put"
    assert doble.llamadas[0]["json"]["routine"]["exercises"] == REMOTO["exercises"]


def test_restaurar_retira_la_marca_de_escritura_en_curso(tmp_path):
    save_backup(tmp_path, "r1", REMOTO)
    marca = pending_marker(tmp_path)
    marca.write_text(json.dumps({"routine_id": "r1"}), encoding="utf-8")
    c, _ = cliente(tmp_path, [FakeResponse(200, {})], write_enabled=False)
    c.restore("r1")
    assert read_pending(tmp_path) is None


def test_sin_copia_no_se_puede_revertir(tmp_path):
    c, _ = cliente(tmp_path, [])
    with pytest.raises(HevyError, match="no hay ninguna copia"):
        c.restore("r1")


def test_una_reversion_rechazada_lanza(tmp_path):
    save_backup(tmp_path, "r1", REMOTO)
    c, _ = cliente(tmp_path, [FakeResponse(403, text="prohibido")])
    with pytest.raises(HevyError, match="403"):
        c.restore("r1")


# ---------------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------------


@dataclass
class SettingsFalsos:
    hevy_api_key: str = "clave"
    hevy_api_base: str = "https://api.hevyapp.com"
    database_url: str = "sqlite:///data/app.db"


def test_build_client_lee_el_interruptor_del_yaml(cfg):
    c = hevy.build_client(SettingsFalsos(), cfg)
    assert c.write_enabled is bool(
        cfg.raw["integrations"]["hevy"].get("write_enabled", False)
    )


def test_build_client_apagado_por_defecto_si_falta_la_seccion():
    c = hevy.build_client(SettingsFalsos(), {})
    assert c.write_enabled is False


def test_build_client_exige_la_clave_en_el_env():
    with pytest.raises(HevyError, match="HEVY_API_KEY"):
        hevy.build_client(SettingsFalsos(hevy_api_key=""), {})
