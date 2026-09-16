"""La base y el YAML hablan del mismo ejercicio y solo uno decide la carga.

QUÉ PASÓ
--------
`gemelo_sentado` quedó guardado como 70 → 60 → 65: el 70 delante era un dedazo
en Hevy que la adopción se creyó. Se corrigió en `config.yaml`... que para un
ejercicio con fila en `exercise_targets` no cambia nada, porque la carga vigente
sale de `current_sets_json` y el fichero no se vuelve a mirar
(`app/engine/progression.py:con_carga_vigente`). Durante cinco días el YAML
decía una rampa y la sesión entrenaba otra, sin un solo error por ningún lado.

Es la figura de siempre en este proyecto: un valor que se lee y que no es el
valor que se usa. No la detecta una batería que compruebe el motor, porque el
motor funcionaba; la detecta algo que COMPARE las dos fuentes.

QUÉ SE ATA AQUÍ
---------------
Lo que se comprueba no es que hoy la base esté bien -eso es un dato, no un test,
y cambia cada vez que se entrena-. Es que el guion SABE ENCONTRAR cada una de
las averías, con la forma exacta que tenían cuando ocurrieron.

Y una cosa más, que es la que de verdad podría pudrirse: `auditar_cargas` se
apoya en una regla inventada -«los pesos efectivos no bajan»- que no es una ley
del entrenamiento sino un hecho de ESTE `config.yaml`. Un hecho puede dejar de
ser cierto. Por eso se comprueba contra el fichero real: el día que alguien
declare a propósito una rampa descendente, este test avisa de que la heurística
ha dejado de valer, en vez de convertir el guion en ruido que se ignora.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.config_loader import load_config
from scripts.auditar_cargas import (
    auditar,
    efectivas,
    ejercicios_del_yaml,
    no_baja,
)


def fila(sets: list[dict] | None):
    """Una fila de `exercise_targets` con lo único que el guion le mira."""
    return SimpleNamespace(
        current_sets_json=json.dumps(sets) if sets is not None else None
    )


def config_falso(exercises: dict[str, list[dict]]):
    """Un config mínimo: `{rutina: [ejercicios]}`, sin heurística de calentamiento."""
    return SimpleNamespace(
        routines={
            rk: {"exercises": exs} for rk, exs in exercises.items()
        },
        set_types={"source": "api", "heuristic": {"enabled": False}},
    )


def normal(reps: int, kg: float) -> dict:
    return {"type": "normal", "reps": reps, "weight_kg": kg}


def texto(lineas: list[str]) -> str:
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# La avería original
# ---------------------------------------------------------------------------


def test_la_rampa_al_reves_se_caza():
    """El caso `gemelo_sentado`, clavado: 70 → 60 → 65 donde el YAML sube."""
    cfg = config_falso(
        {"dia_1": [{"key": "gemelo_sentado", "sets": [normal(12, 60), normal(15, 65), normal(12, 70)]}]}
    )
    filas = {("dia_1", "gemelo_sentado"): fila([normal(12, 70), normal(12, 60), normal(15, 65)])}
    problemas, _ = auditar(cfg, filas)
    assert any("RAMPA AL REVÉS" in p and "gemelo_sentado" in p for p in problemas)


def test_la_rampa_ya_ordenada_no_dice_nada():
    """Y una vez corregida se calla, que es la otra mitad de servir para algo."""
    cfg = config_falso(
        {"dia_1": [{"key": "gemelo_sentado", "sets": [normal(12, 60), normal(15, 65), normal(12, 70)]}]}
    )
    filas = {("dia_1", "gemelo_sentado"): fila([normal(12, 60), normal(15, 65), normal(12, 70)])}
    problemas, _ = auditar(cfg, filas)
    assert problemas == []


def test_una_rampa_descendente_a_proposito_desactiva_su_propio_aviso():
    """La heurística se valida contra el fichero antes de usarse.

    Si el YAML declara los pesos de más a menos, ese ejercicio deja de mirarse
    por orden y se DICE que ha dejado de mirarse. Una comprobación que no se
    puede desmentir acaba desactivada entera.
    """
    cfg = config_falso(
        {"dia_1": [{"key": "descendente", "sets": [normal(8, 80), normal(10, 70), normal(12, 60)]}]}
    )
    filas = {("dia_1", "descendente"): fila([normal(8, 80), normal(10, 70), normal(12, 60)])}
    problemas, notas = auditar(cfg, filas)
    assert problemas == []
    assert any("RAMPA " in n and "descendente" in n for n in notas)


def test_un_ejercicio_sin_pesos_no_se_mira_por_orden():
    """Planchas y bird dogs no tienen carga: ordenarlos por kilos no significa nada."""
    sets = [{"reps": 20}, {"reps": 20}, {"reps": 20}]
    cfg = config_falso({"dia_1": [{"key": "perro_de_caza", "sets": sets}]})
    problemas, _ = auditar(cfg, {("dia_1", "perro_de_caza"): fila(sets)})
    assert problemas == []


# ---------------------------------------------------------------------------
# Las otras cuatro
# ---------------------------------------------------------------------------


def test_la_huerfana_dice_en_que_rutina_vive_de_verdad():
    """Cinco filas `dia_1/…` eran ejercicios de `hiit_dia_1`: las dos rutinas se
    registraron en un mismo entreno de Hevy y el ejercicio se apuntó a la del
    título. Desde la fila sola no se distingue de un ejercicio retirado, y las
    dos cosas se arreglan distinto, así que el aviso lo dice."""
    cfg = config_falso(
        {
            "dia_1": [{"key": "prensa", "sets": [normal(12, 100)]}],
            "hiit_dia_1": [{"key": "wall_ball", "sets": [normal(20, 5)]}],
        }
    )
    problemas, _ = auditar(cfg, {("dia_1", "wall_ball"): fila(None)})
    assert any("HUÉRFANA" in p and "hiit_dia_1" in p for p in problemas)


def test_la_huerfana_de_una_clave_retirada_no_inventa_una_rutina():
    cfg = config_falso({"dia_1": [{"key": "prensa", "sets": [normal(12, 100)]}]})
    problemas, _ = auditar(cfg, {("dia_1", "ejercicio_retirado"): fila([normal(12, 50)])})
    assert any("no está en ninguna rutina" in p for p in problemas)


def test_por_debajo_del_arranque_del_yaml_se_caza():
    """Se progresa hacia arriba: aparecer por debajo del punto de partida
    significa que algo la sembró mal o la deshizo."""
    cfg = config_falso({"dia_1": [{"key": "prensa", "sets": [normal(12, 100), normal(12, 120)]}]})
    filas = {("dia_1", "prensa"): fila([normal(12, 60), normal(12, 80)])}
    problemas, _ = auditar(cfg, filas)
    assert any("POR DEBAJO" in p for p in problemas)


def test_las_reps_fuera_del_rep_range_se_cazan():
    """La progresión decide mirando ese rango; con las reps fuera, la puerta se
    abre o se cierra por un motivo que no es el suyo."""
    cfg = config_falso(
        {"dia_1": [{"key": "prensa", "rep_range": [10, 12], "sets": [normal(12, 100)]}]}
    )
    problemas, _ = auditar(cfg, {("dia_1", "prensa"): fila([normal(20, 100)])})
    assert any("REPS FUERA" in p for p in problemas)


def test_subir_de_carga_es_normal_y_no_es_un_problema():
    cfg = config_falso({"dia_1": [{"key": "prensa", "sets": [normal(12, 100)]}]})
    problemas, notas = auditar(cfg, {("dia_1", "prensa"): fila([normal(12, 120)])})
    assert problemas == []
    assert any("SUBIDA" in n for n in notas)


def test_un_ejercicio_sin_fila_se_cuenta_pero_no_es_un_problema():
    """Es el único caso en que editar el YAML sirve de algo, y por eso se lista."""
    cfg = config_falso({"dia_1": [{"key": "prensa", "sets": [normal(12, 100)]}]})
    problemas, notas = auditar(cfg, {})
    assert problemas == []
    assert any("SIN FILA" in n and "prensa" in n for n in notas)


def test_un_json_ilegible_se_dice_en_vez_de_reventar():
    cfg = config_falso({"dia_1": [{"key": "prensa", "sets": [normal(12, 100)]}]})
    rota = SimpleNamespace(current_sets_json="{no es json")
    problemas, _ = auditar(cfg, {("dia_1", "prensa"): rota})
    assert any("ILEGIBLE" in p for p in problemas)


# ---------------------------------------------------------------------------
# La heurística, contra el fichero de verdad
# ---------------------------------------------------------------------------


def test_en_el_config_real_ninguna_rampa_baja(cfg):
    """El hecho en el que se apoya «RAMPA AL REVÉS», comprobado donde vive.

    Si este test empieza a fallar NO es que el guion esté mal: es que el
    programa ha cambiado y la heurística ha dejado de describirlo. Entonces hay
    que decidir si el aviso sigue valiendo, no silenciarlo.
    """
    descendentes = [
        clave
        for clave, ex in ejercicios_del_yaml(cfg).items()
        if (efs := efectivas(ex, cfg.set_types)) and not no_baja(efs)
    ]
    assert descendentes == [], (
        "algún ejercicio declara los pesos de más a menos; la heurística de "
        "`auditar_cargas` asumía que eso no pasaba"
    )


def test_el_config_real_pasa_su_propia_auditoria_sin_filas(cfg):
    """Sin ninguna carga guardada, el YAML solo no puede contradecirse a sí mismo."""
    problemas, _ = auditar(cfg, {})
    assert problemas == []
