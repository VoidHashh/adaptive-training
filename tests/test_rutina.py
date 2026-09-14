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

    def __init__(self, estalla: Exception | None = None, remoto: dict | None = None):
        self.llamadas: list[tuple[str, Backup | None]] = []
        self.lecturas: list[str] = []
        self.estalla = estalla
        # Lo que devuelve el GET. `None` porque la mayoría de los tests de
        # `revertir` no leen nada, y dejar un diccionario de relleno haría que
        # pareciera que sí.
        self.remoto = remoto

    def restore(self, routine_id: str, backup: Backup | None = None):
        self.llamadas.append((routine_id, backup))
        if self.estalla:
            raise self.estalla

        class R:
            reason = "revertida al estado de 2026-09-14 07:05:00"

        return R()

    def get_routine(self, routine_id: str):
        """Lo que Hevy contesta AHORA MISMO, para poder cerrar la marca.

        Se apunta la llamada igual que las de `restore`, pero en su propia
        lista: `cerrar` existe precisamente para NO escribir, así que un test
        que mire `llamadas` y lo encuentre vacío está comprobando lo que debe.
        Meter el GET en el mismo sitio que el PUT haría que «no ha tocado
        nada» y «no ha mirado nada» se leyeran igual, y son cosas distintas.
        """
        self.lecturas.append(routine_id)
        if self.estalla:
            raise self.estalla
        return self.remoto


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
# cerrar: retirar la marca SIN escribir en Hevy
# ---------------------------------------------------------------------------
#
# POR QUÉ EXISTE ESTE COMANDO Y POR QUÉ SE PRUEBA TANTO
# ------------------------------------------------------
# El 2026-09-14 la escritura de las 09:00 falló -Hevy contestó 400 porque el
# cuerpo llevaba `"notes": []`- y la marca se quedó puesta. La rutina de Hevy
# estaba INTACTA: su `updated_at` seguía siendo el del día 8. O sea que no había
# nada roto, solo un aviso en la pantalla que ya no describía nada.
#
# Y para quitarlo, las dos únicas salidas eran malas: `revertir`, que manda un
# PUT para escribir la rutina con exactamente lo que ya tiene -riesgo a cambio
# de nada, y encima por el mismo camino que acababa de fallar-, o entrar en el
# contenedor y borrar el fichero a mano, que es saltarse la comprobación entera
# y decidir de memoria.
#
# El riesgo de un comando así es evidente: si se convierte en un botón de «ya lo
# he mirado», el día que la escritura SÍ haya llegado a medias alguien lo pulsa
# por costumbre y se queda con una rutina mezclada y sin aviso. Por eso casi
# todos los tests de abajo comprueban que NO cierra.


def _marca(raiz: Path, **campos) -> Path:
    """Escribe la marca de escritura en curso con la forma que usa `write_routine`."""
    m = pending_marker(raiz)
    m.parent.mkdir(parents=True, exist_ok=True)
    base = {"routine_id": DIA_1, "started_at": "2026-09-14T09:00:18"}
    base.update(campos)
    m.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return m


def _remoto(ejercicios: int = 2, **extra) -> dict:
    """Lo que devuelve el GET de Hevy: la rutina más la morralla del formato.

    `id`, `index`, `updated_at` y compañía vienen SIEMPRE en la respuesta y no
    están en la copia. Van aquí a propósito: si la comparación se hiciera sobre
    el JSON crudo, estos campos harían que toda rutina pareciese distinta de su
    copia y el comando mandaría a revertir siempre.
    """
    r = {
        "id": DIA_1,
        "updated_at": "2026-09-08T06:23:34.137Z",
        "folder_id": None,
        "title": "Día 1",
        "notes": None,
        "exercises": [
            {"exercise_template_id": f"E{i}", "index": i, "title": f"Ejercicio {i}"}
            for i in range(ejercicios)
        ],
    }
    r.update(extra)
    return r


def test_cerrar_sin_marca_no_hace_nada_y_no_llama_a_hevy(cfg, raiz, monkeypatch, capsys):
    falso = ClienteFalso()
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_cerrar(cfg, _args(si=True)) == 0
    assert falso.lecturas == [], "ha ido a Hevy sin haber nada que comprobar"
    assert "No hay ninguna marca" in capsys.readouterr().out


def test_cerrar_retira_la_marca_cuando_la_rutina_es_la_de_la_copia(
    cfg, raiz, monkeypatch, capsys
):
    """El caso del 2026-09-14: el PUT no llegó a aplicar nada.

    Lo que se comprueba no es solo que la marca desaparezca, sino que NO se haya
    escrito en Hevy para conseguirlo. Cerrar escribiendo sería `revertir` con
    otro nombre.
    """
    copia = guardar_copia(raiz, DIA_1, "20260914-090018.json")
    _marca(raiz, backup=str(copia))
    falso = ClienteFalso(remoto=_remoto())
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_cerrar(cfg, _args(si=True)) == 0
    assert not pending_marker(raiz).exists(), "la marca sigue puesta"
    assert falso.llamadas == [], "ha ESCRITO en Hevy para cerrar una marca"
    assert falso.lecturas == [DIA_1], "ha cerrado sin mirar qué hay en Hevy"
    salida = capsys.readouterr().out
    assert "No hay nada que revertir" in salida
    assert "Marca retirada" in salida


def test_cerrar_NO_retira_la_marca_si_la_rutina_ha_cambiado(
    cfg, raiz, monkeypatch, capsys
):
    """Si la escritura sí llegó, el aviso tenía razón y se queda.

    Éste es el test que impide que el comando degenere en un «ya lo he mirado».
    La rutina de Hevy tiene tres ejercicios y la copia dos: el PUT entró, entero
    o a medias, y aquí no hay nada que cerrar.
    """
    copia = guardar_copia(raiz, DIA_1, "20260914-090018.json", ejercicios=2)
    _marca(raiz, backup=str(copia))
    falso = ClienteFalso(remoto=_remoto(ejercicios=3))
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_cerrar(cfg, _args(si=True)) == 1
    assert pending_marker(raiz).exists(), (
        "ha retirado la marca con la rutina cambiada: el aviso desaparece y la "
        "rutina mezclada se queda"
    )
    salida = capsys.readouterr().out
    assert "NO ES LA DE LA COPIA" in salida
    assert "revertir --rutina dia_1" in salida, "no dice cómo deshacerlo"


def test_cerrar_no_confunde_la_morralla_del_GET_con_un_cambio(
    cfg, raiz, monkeypatch, capsys
):
    """La comparación va sobre `cuerpo_para_put`, no sobre el JSON crudo.

    El GET devuelve `id`, `updated_at`, `index` y los títulos de cada ejercicio;
    la copia guarda lo que se manda en el PUT. Comparando el crudo, una rutina
    intacta parecería distinta de su propia copia SIEMPRE, y este comando -que
    existe para no escribir- mandaría a revertir cada vez. Que es el peor
    desenlace posible: un consejo de escribir en Hevy, con confianza, y sin
    motivo.
    """
    copia = guardar_copia(raiz, DIA_1, "20260914-090018.json")
    _marca(raiz, backup=str(copia))
    falso = ClienteFalso(remoto=_remoto(otro_campo_nuevo_de_hevy="lo que sea"))
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_cerrar(cfg, _args(si=True)) == 0, (
        f"campos que solo trae el GET se han leído como un cambio real: "
        f"{capsys.readouterr().out}"
    )


def test_cerrar_si_no_se_puede_leer_hevy_la_marca_se_queda(
    cfg, raiz, monkeypatch, capsys
):
    """No saber no es lo mismo que estar bien.

    Un `except` que cerrara la marca aquí convertiría un fallo de red en un
    certificado de que todo está en orden, que es el patrón que este proyecto
    lleva persiguiendo desde el principio.
    """
    copia = guardar_copia(raiz, DIA_1, "20260914-090018.json")
    _marca(raiz, backup=str(copia))
    falso = ClienteFalso(estalla=HevyError("la API no contesta"))
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_cerrar(cfg, _args(si=True)) == 1
    assert pending_marker(raiz).exists()
    assert "no saber no es lo mismo que estar bien" in capsys.readouterr().out.lower()


def test_cerrar_con_la_copia_ilegible_no_cierra_nada(cfg, raiz, monkeypatch, capsys):
    """Sin copia no hay con qué comparar, así que no hay nada que concluir."""
    copia = guardar_copia(raiz, DIA_1, "20260914-090018.json")
    copia.write_text("{roto", encoding="utf-8")
    _marca(raiz, backup=str(copia))
    falso = ClienteFalso(remoto=_remoto())
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_cerrar(cfg, _args(si=True)) == 1
    assert pending_marker(raiz).exists()
    assert falso.lecturas == [], "ha ido a Hevy sin tener con qué comparar"
    assert "no se cierra nada" in capsys.readouterr().out


def test_cerrar_con_una_marca_que_no_dice_de_que_rutina_no_cierra_a_ciegas(
    cfg, raiz, monkeypatch, capsys
):
    """`read_pending` devuelve `routine_id: "?"` cuando la marca está corrupta.

    Ese `?` es un valor centinela, no un id, y tratarlo como un id llevaría a
    pedirle a Hevy la rutina `?`. Se para antes y manda a mirar a mano, que es
    lo único honesto cuando el fichero que dice qué pasó es ilegible.
    """
    m = pending_marker(raiz)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("{esto no es json", encoding="utf-8")
    falso = ClienteFalso(remoto=_remoto())
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    assert rutina.cmd_cerrar(cfg, _args(si=True)) == 1
    assert pending_marker(raiz).exists()
    assert falso.lecturas == []
    assert "No se cierra a ciegas" in capsys.readouterr().out


def test_cerrar_sin_confirmar_deja_la_marca_puesta(cfg, raiz, monkeypatch, capsys):
    """Aunque todo esté bien, sin escribir CERRAR no se cierra.

    La comprobación sale verde y aun así hace falta la confirmación: lo que se
    retira es el único rastro de que hubo una escritura indeterminada.
    """
    copia = guardar_copia(raiz, DIA_1, "20260914-090018.json")
    _marca(raiz, backup=str(copia))
    falso = ClienteFalso(remoto=_remoto())
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)
    monkeypatch.setattr("builtins.input", lambda _: "si")  # no es CERRAR

    assert rutina.cmd_cerrar(cfg, _args(si=False)) == 1
    assert pending_marker(raiz).exists()
    assert "cancelado" in capsys.readouterr().out


def test_cerrar_cancelado_con_ctrl_c_deja_la_marca_puesta(cfg, raiz, monkeypatch):
    copia = guardar_copia(raiz, DIA_1, "20260914-090018.json")
    _marca(raiz, backup=str(copia))
    falso = ClienteFalso(remoto=_remoto())
    monkeypatch.setattr("app.integrations.hevy.build_client", lambda *a, **k: falso)

    def corta(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", corta)

    assert rutina.cmd_cerrar(cfg, _args(si=False)) == 1
    assert pending_marker(raiz).exists()


# ---------------------------------------------------------------------------


def _args(**kw):
    """Un `Namespace` con los valores por defecto de argparse."""
    import argparse

    base = {"rutina": None, "copia": None, "si": False, "n": 5}
    base.update(kw)
    return argparse.Namespace(**base)
