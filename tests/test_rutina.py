"""La herramienta de operador: mirar las copias, y deshacer una escritura.

POR QUÉ ESTOS TESTS Y NO OTROS
------------------------------
`HevyClient.restore()` ya tenía seis tests en `test_hevy.py` y todos pasaban.
Eso no impidió que la función fuera **inalcanzable**: no la llamaba nadie fuera
de los tests. Lo que aquí se prueba no es que la reversión funcione -eso ya
estaba probado- sino que se pueda EJECUTAR: que exista el comando, que traduzca
`dia_1` al id correcto, que pida confirmación antes de tocar Hevy, y sobre todo
que se niegue a hacer nada cuando no hay a dónde volver.

El criterio es el del proyecto: los fallos silenciosos son los peligrosos. Un
`revertir` que no encuentra copia y contesta con buena cara sería exactamente el
fallo que esta herramienta existe para evitar, así que casi todos los tests de
abajo comprueban negativas: que NO se llama a Hevy.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app import rutina
from app.integrations.hevy import Backup, HevyError, backup_dir, pending_marker


# ---------------------------------------------------------------------------
# Andamiaje
# ---------------------------------------------------------------------------


class ClienteFalso:
    """Un Hevy que no existe. Apunta si le piden revertir, y qué."""

    def __init__(self, estalla: Exception | None = None):
        self.llamadas: list[tuple[str, Backup | None]] = []
        self.estalla = estalla

    def restore(self, routine_id: str, backup: Backup | None = None):
        self.llamadas.append((routine_id, backup))
        if self.estalla:
            raise self.estalla

        class R:
            reason = "revertida al estado de 2026-09-14 07:05:00"

        return R()


def guardar_copia(raiz: Path, rid: str, nombre: str, ejercicios: int = 2) -> Path:
    """Escribe una copia con la MISMA forma que `save_backup`.

    La forma se copia del código de producción a propósito, no se inventa: en
    la primera versión de la herramienta supuse que la clave era `payload` y en
    realidad es `routine`, y el error habría aparecido el día de revertir.
    """
    carpeta = backup_dir(raiz, rid)
    carpeta.mkdir(parents=True, exist_ok=True)
    f = carpeta / nombre
    f.write_text(
        json.dumps(
            {
                "routine_id": rid,
                "taken_at": datetime(2026, 9, 14, 7, 5, 0).isoformat(),
                "routine": {
                    "title": "Día 1",
                    "notes": None,
                    "exercises": [{"exercise_template_id": f"E{i}"} for i in range(ejercicios)],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return f


@pytest.fixture
def raiz(tmp_path, monkeypatch):
    """Una carpeta de datos de mentira, para no tocar la del usuario."""
    monkeypatch.setattr(rutina, "data_root", lambda: tmp_path)
    return tmp_path


DIA_1 = "29ce5818-5442-4a40-9e70-1e74904d5867"


# ---------------------------------------------------------------------------
# Traducir `dia_1` a un id
# ---------------------------------------------------------------------------


def test_resolver_acepta_la_clave_y_el_id(cfg):
    """Las dos formas, porque las dos aparecen.

    La clave es lo que uno recuerda; el id es lo que sale en los mensajes de
    error y en los nombres de las carpetas. Traducir a mano entre ellas, con la
    rutina rota y con prisa, es pedir un error de más.
    """
    clave, rid = rutina.resolver(cfg, "dia_1")
    assert clave == "dia_1" and rid == DIA_1
    assert rutina.resolver(cfg, rid) == ("dia_1", DIA_1)


def test_resolver_una_rutina_que_no_existe_para_el_programa(cfg):
    """Y dice cuáles hay, en vez de solo que esa no."""
    with pytest.raises(SystemExit) as e:
        rutina.resolver(cfg, "dia_47")
    assert "dia_1" in str(e.value)


def test_las_rutinas_sin_id_de_hevy_no_se_ofrecen(cfg_copia):
    """Ofrecer algo que va a fallar más tarde es peor que no ofrecerlo.

    Una rutina sin `hevy_routine_id` no se escribe nunca, así que tampoco se
    puede revertir. Si apareciera en la lista, el fallo llegaría en el momento
    de usarla.
    """
    cfg_copia.raw["routines"]["dia_1"]["hevy_routine_id"] = None
    assert "dia_1" not in rutina.routine_map(cfg_copia)
    assert "dia_2" in rutina.routine_map(cfg_copia)


# ---------------------------------------------------------------------------
# revertir: cuándo NO toca Hevy
# ---------------------------------------------------------------------------


def test_revertir_sin_ninguna_copia_no_llama_a_hevy(cfg, raiz, monkeypatch, capsys):
    """Sin copia no hay a dónde volver, y hay que decirlo antes de tocar nada."""
    falso = ClienteFalso()
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    args = _args(rutina=DIA_1, copia=None, si=True)
    assert rutina.cmd_revertir(cfg, args) == 1
    assert falso.llamadas == [], "ha llamado a Hevy sin tener copia"
    assert "no se ha tocado nada" in capsys.readouterr().out


def test_revertir_con_la_copia_corrupta_dice_que_esta_corrupta(cfg, raiz, monkeypatch, capsys):
    """«No hay copia» y «la copia está rota» mandan a buscar a sitios distintos.

    Si la única copia es ilegible y el mensaje dice que no hay ninguna, el
    operador va a mirar por qué no se guardó -y sí se guardó-. Es un minuto
    perdido en el peor momento posible.
    """
    f = guardar_copia(raiz, DIA_1, "20260914-070500.json")
    f.write_text("{esto no es json", encoding="utf-8")
    falso = ClienteFalso()
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_revertir(cfg, _args(rutina="dia_1", copia=str(f), si=True)) == 1
    assert falso.llamadas == []
    assert "no se puede leer" in capsys.readouterr().out


def test_revertir_con_un_fichero_que_no_existe(cfg, raiz, monkeypatch, capsys):
    falso = ClienteFalso()
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)
    args = _args(rutina="dia_1", copia=str(raiz / "no-existe.json"), si=True)

    assert rutina.cmd_revertir(cfg, args) == 1
    assert falso.llamadas == []
    assert "no existe el fichero" in capsys.readouterr().out


def test_revertir_sin_confirmar_no_toca_hevy(cfg, raiz, monkeypatch, capsys):
    """Escribir REVERTIR o no pasa nada.

    Quien llega aquí llega con prisa. Una tecla de más es barata comparada con
    revertir la rutina equivocada.
    """
    guardar_copia(raiz, DIA_1, "20260914-070500.json")
    falso = ClienteFalso()
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)
    monkeypatch.setattr("builtins.input", lambda _: "si")  # no es REVERTIR

    assert rutina.cmd_revertir(cfg, _args(rutina="dia_1", copia=None, si=False)) == 1
    assert falso.llamadas == []
    assert "cancelado" in capsys.readouterr().out


def test_revertir_cancelado_con_ctrl_c_no_toca_hevy(cfg, raiz, monkeypatch):
    guardar_copia(raiz, DIA_1, "20260914-070500.json")
    falso = ClienteFalso()
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    def corta(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", corta)
    assert rutina.cmd_revertir(cfg, _args(rutina="dia_1", copia=None, si=False)) == 1
    assert falso.llamadas == []


# ---------------------------------------------------------------------------
# revertir: cuándo SÍ
# ---------------------------------------------------------------------------


def test_revertir_confirmado_usa_la_copia_mas_reciente(cfg, raiz, monkeypatch, capsys):
    """Por defecto se vuelve a lo último que había antes de la última escritura."""
    guardar_copia(raiz, DIA_1, "20260913-070000.json", ejercicios=1)
    guardar_copia(raiz, DIA_1, "20260914-070500.json", ejercicios=9)
    falso = ClienteFalso()
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)
    monkeypatch.setattr("builtins.input", lambda _: "REVERTIR")

    assert rutina.cmd_revertir(cfg, _args(rutina="dia_1", copia=None, si=False)) == 0
    assert len(falso.llamadas) == 1
    rid, copia = falso.llamadas[0]
    assert rid == DIA_1
    assert copia.path.name == "20260914-070500.json", "no ha usado la más reciente"
    assert len(copia.payload["exercises"]) == 9
    assert "REVERTIDA" in capsys.readouterr().out


def test_revertir_con_copia_elegida_a_mano_se_salta_la_ultima(cfg, raiz, monkeypatch):
    """El caso de saltarse una escritura mala: volver dos días atrás, no uno.

    Sin esto, si la escritura de ayer ya era mala, revertir devolvería a ayer y
    el problema seguiría ahí.
    """
    vieja = guardar_copia(raiz, DIA_1, "20260913-070000.json", ejercicios=1)
    guardar_copia(raiz, DIA_1, "20260914-070500.json", ejercicios=9)
    falso = ClienteFalso()
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_revertir(cfg, _args(rutina="dia_1", copia=str(vieja), si=True)) == 0
    _, copia = falso.llamadas[0]
    assert copia.path.name == "20260913-070000.json"
    assert len(copia.payload["exercises"]) == 1


def test_si_hevy_falla_el_revertido_lo_dice_y_devuelve_error(cfg, raiz, monkeypatch, capsys):
    """Un revertido fallido que contesta 0 es lo peor que podría pasar aquí.

    El operador daría por hecho que la rutina ya está bien y se iría a entrenar
    con la rutina rota.
    """
    guardar_copia(raiz, DIA_1, "20260914-070500.json")
    falso = ClienteFalso(estalla=HevyError("la reversión devolvió 500"))
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_revertir(cfg, _args(rutina="dia_1", copia=None, si=True)) == 1
    salida = capsys.readouterr().out
    assert "NO se ha revertido" in salida and "500" in salida


# ---------------------------------------------------------------------------
# estado y copias
# ---------------------------------------------------------------------------


def test_estado_saca_la_escritura_a_medias(cfg, raiz, capsys):
    """La marca que nadie leía, ahora también aquí.

    `/api/health` la publica, pero el operador que va a revertir está en una
    consola, no mirando un JSON.
    """
    marca = pending_marker(raiz)
    marca.parent.mkdir(parents=True, exist_ok=True)
    marca.write_text(json.dumps({"routine_id": DIA_1, "started_at": "2026-09-14T07:05"}), encoding="utf-8")

    assert rutina.cmd_estado(cfg, _args()) == 0
    salida = capsys.readouterr().out
    assert "ESCRITURA SIN CONFIRMAR" in salida
    assert DIA_1 in salida, "no dice qué rutina quedó a medias"


def test_estado_sin_marca_dice_que_no_hay(cfg, raiz, capsys):
    assert rutina.cmd_estado(cfg, _args()) == 0
    assert "no hay escrituras a medias" in capsys.readouterr().out


def test_estado_cuenta_las_copias_de_cada_rutina(cfg, raiz, capsys):
    guardar_copia(raiz, DIA_1, "20260913-070000.json")
    guardar_copia(raiz, DIA_1, "20260914-070500.json")

    assert rutina.cmd_estado(cfg, _args()) == 0
    salida = capsys.readouterr().out
    fila = [l for l in salida.splitlines() if l.startswith("dia_1")][0]
    assert "2" in fila and "2026-09-14" in fila
    assert "nunca escrita" in [l for l in salida.splitlines() if l.startswith("dia_2")][0]


def test_copias_marca_la_ilegible_en_vez_de_saltarsela(cfg, raiz, capsys):
    """Una copia rota se DICE, y se dice al listarla.

    Callarla la dejaría en la lista como una opción válida hasta el momento de
    usarla, que es siempre el peor momento para descubrirlo.
    """
    guardar_copia(raiz, DIA_1, "20260913-070000.json")
    mala = guardar_copia(raiz, DIA_1, "20260912-070000.json")
    mala.write_text("{roto", encoding="utf-8")

    assert rutina.cmd_copias(cfg, _args(rutina="dia_1")) == 0
    salida = capsys.readouterr().out
    assert "ILEGIBLE" in salida
    assert "20260913-070000.json" in salida
    assert "la que usaría `revertir`" in salida


def test_copias_sin_ninguna_copia_lo_explica(cfg, raiz, capsys):
    assert rutina.cmd_copias(cfg, _args(rutina="dia_1")) == 0
    assert "no hay ninguna copia" in capsys.readouterr().out


# ---------------------------------------------------------------------------


def _args(**kw):
    """Un `Namespace` con los valores por defecto de argparse."""
    import argparse

    base = {"rutina": None, "copia": None, "si": False, "n": 5}
    base.update(kw)
    return argparse.Namespace(**base)
