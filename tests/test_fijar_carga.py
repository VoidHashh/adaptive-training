"""La salida de emergencia de la adopción: fijar una carga a mano.

El tope de salto para la sesión sin adoptar y lo dice, que es lo correcto para
una errata al teclear en Hevy. Pero cuando el salto era REAL -60 kg de verdad en
una extensión cuyo objetivo eran 40- hacía falta una forma de cerrarlo, porque
el consejo que daba el mensaje («se sube a mano en config.yaml») apuntaba a una
pieza que ya no decide nada: en cuanto un ejercicio tiene fila en
`exercise_targets`, la carga sale de ahí y el YAML no se vuelve a mirar.
"""

from __future__ import annotations

import pytest

from app.config_loader import load_config
from app.models import ExerciseTarget
from tests.conftest import con_bloque_hiit
from tests.dobles import doble_de
from scripts.fijar_carga import (
    CargaInvalida,
    efectivas_del_yaml,
    huerfanas,
    nuevas_series,
    parsea_series,
)


def series(*pesos: float) -> list[dict[str, object]]:
    return [{"type": "normal", "reps": 12, "weight_kg": p} for p in pesos]


def fijar(vigentes, texto):
    """El camino de verdad: lo que se teclea, parseado y aplicado."""
    return nuevas_series(vigentes, parsea_series(texto))


def test_cambia_los_pesos_y_deja_las_reps_donde_estaban():
    """Esto sube un peso. Tocar las reps de paso sería cambiar el entrenamiento
    por un camino que nadie ha pedido."""
    salida = fijar(series(30, 35, 40), "50,55,60")
    assert [s["weight_kg"] for s in salida] == [50, 55, 60]
    assert [s["reps"] for s in salida] == [12, 12, 12]


def test_no_se_puede_cambiar_el_numero_de_series_por_el_numero_de_comas():
    """Rellenar o recortar cambiaría el ESQUEMA del ejercicio -de tres series a
    dos- con la excusa de cambiarle el peso. Un cambio así se pide a la cara."""
    with pytest.raises(CargaInvalida, match="3 series efectivas"):
        fijar(series(30, 35, 40), "50,60")


def test_un_peso_negativo_no_es_una_carga():
    with pytest.raises(CargaInvalida, match="negativo"):
        fijar(series(30), "-5")


def test_sin_series_de_las_que_partir_se_niega():
    """Sin nada de lo que partir no hay reps que conservar, y el guión no se las
    puede inventar."""
    with pytest.raises(CargaInvalida, match="no tiene series efectivas"):
        fijar([], "50")


# ---------------------------------------------------------------------------
# Reordenar una rampa. Ver el docstring de `parsea_series`.
# ---------------------------------------------------------------------------


def gemelo() -> list[dict[str, object]]:
    """La rampa de `gemelo_sentado` tal y como estaba en la base: con el dedazo.

    El 70 delante era un error de tecleo en Hevy que la adopción se creyó, y las
    15 repeticiones iban con el 65.
    """
    return [
        {"type": "normal", "reps": 12, "weight_kg": 70},
        {"type": "normal", "reps": 12, "weight_kg": 60},
        {"type": "normal", "reps": 15, "weight_kg": 65},
    ]


def test_ordenar_solo_los_kilos_arrastra_las_reps_al_peso_mayor():
    """El fallo que motivó la sintaxis nueva, clavado como test.

    `60,65,70` parece la corrección obvia y no lo es: las reps se quedan donde
    estaban, así que las 15 -que iban con el 65- acaban en la serie de 70. Nadie
    pidió un ejercicio más duro; se colaría por la puerta de atrás de una
    corrección de orden. Esto NO es un fallo del guión, es lo que «cambiar los
    pesos» significa; por eso se comprueba, para que quede escrito cuál es la
    forma equivocada.
    """
    salida = fijar(gemelo(), "60,65,70")
    assert [s["weight_kg"] for s in salida] == [60, 65, 70]
    assert [s["reps"] for s in salida] == [12, 12, 15]
    assert salida[-1]["reps"] == 15, "las 15 reps han caído en el peso más alto"


def test_con_peso_y_reps_la_rampa_se_reordena_entera():
    """La forma correcta: los mismos tres pares, puestos de menos a más."""
    salida = fijar(gemelo(), "60x12,65x15,70x12")
    assert [(s["weight_kg"], s["reps"]) for s in salida] == [(60, 12), (65, 15), (70, 12)]

    antes = sorted((s["weight_kg"], s["reps"]) for s in gemelo())
    assert sorted((s["weight_kg"], s["reps"]) for s in salida) == antes, (
        "reordenar no puede inventar ni perder ninguna serie"
    )


def test_la_rampa_reordenada_es_la_que_declara_el_config(cfg):
    """Y coincide con el YAML, que es el punto de la corrección entera."""
    delyaml = efectivas_del_yaml(cfg, "dia_1", "gemelo_sentado")
    salida = fijar(gemelo(), "60x12,65x15,70x12")
    assert [(s["weight_kg"], s["reps"]) for s in salida] == [
        (s["weight_kg"], s["reps"]) for s in delyaml
    ]


def test_la_aspa_del_teclado_espanol_vale_igual():
    """Quien copie la rampa de un comentario del YAML traerá «×», no «x»."""
    assert parsea_series("60×12,65×15") == parsea_series("60x12,65x15")


def test_mezclar_las_dos_formas_se_rechaza():
    """`60,65x15,70` es ambiguo justo donde más da igual equivocarse: las reps de
    las otras dos dependerían del orden viejo, que es lo que se está cambiando."""
    with pytest.raises(CargaInvalida, match="o todas las series llevan repeticiones"):
        parsea_series("60,65x15,70")


def test_una_serie_de_cero_repeticiones_no_es_una_serie():
    with pytest.raises(CargaInvalida, match="cero repeticiones"):
        fijar(gemelo(), "60x12,65x0,70x12")


def test_el_punto_de_partida_sale_del_yaml_sin_el_calentamiento(cfg):
    """Para un ejercicio que todavía no ha progresado nunca, el YAML sí manda.

    Y el calentamiento no cuenta: sale del fichero en cada construcción y la
    progresión tampoco lo toca.
    """
    efectivas = efectivas_del_yaml(cfg, "dia_1", "extension_cuadriceps")
    assert efectivas, "el ejercicio está en esa rutina del config real"
    assert all(str(s.get("type") or "normal") != "warmup" for s in efectivas)


def test_un_ejercicio_que_no_esta_en_esa_rutina_no_inventa_series():
    c = load_config("config.yaml")
    assert efectivas_del_yaml(c, "dia_1", "ejercicio_fantasma") == []
    assert efectivas_del_yaml(c, "rutina_fantasma", "extension_cuadriceps") == []


# ---------------------------------------------------------------------------
# Las huérfanas
#
# El 2026-09-15 se registró el Día 1 y el bloque HIIT como UN SOLO
# entrenamiento de Hevy. La reconciliación atribuye el día entero a la rutina
# de fuerza, así que `apply_execution` y `adoptar_cargas` recibieron
# `routine_key="dia_1"` con una lista de ejercicios que llevaba pegados los
# cinco del bloque, y sembraron cinco filas `dia_1/remo_maquina`,
# `dia_1/suitcase_carry`, `dia_1/air_bike`, `dia_1/plancha_frontal` y
# `dia_1/wall_ball`. Esas claves no existen en `dia_1`: existen en
# `hiit_dia_1`. Nadie las vuelve a leer nunca.
# ---------------------------------------------------------------------------


@doble_de(ExerciseTarget)
class _FilaFalsa:
    """Lo justo que mira `huerfanas`: la clave. El resto de la fila no lo toca.

    Se declara doble de `ExerciseTarget` y no ayudante suelto porque eso es lo
    que es: si mañana la fila deja de tener `routine_key` o `exercise_key`,
    quiero que salte aquí y no que estos tests sigan en verde midiendo una
    columna que ya no existe.
    """

    def __init__(self, routine_key: str, exercise_key: str) -> None:
        self.routine_key, self.exercise_key = routine_key, exercise_key


def _filas(*claves: tuple[str, str]) -> dict[tuple[str, str], object]:
    return {k: _FilaFalsa(*k) for k in claves}


def test_la_huerfana_se_detecta_por_el_par_y_no_por_el_ejercicio(cfg):
    """El caso real, y el que un chequeo por clave suelta daría por bueno.

    `remo_maquina` SÍ está en el `config.yaml`: está en `hiit_dia_1`. Mirar solo
    la clave del ejercicio lo encontraría, diría que todo cuadra y dejaría la
    fila colgada de `dia_1` para siempre. Lo que está mal no es el ejercicio, es
    de qué rutina cuelga.
    """
    cfg = con_bloque_hiit(cfg)
    sobran = huerfanas(cfg, _filas(("dia_1", "remo_maquina")))
    assert sobran == [("dia_1", "remo_maquina")]
    assert huerfanas(cfg, _filas(("hiit_dia_1", "remo_maquina"))) == []


def test_las_cinco_del_15_de_septiembre_son_huerfanas_y_en_su_sitio_no(cfg):
    """Las cinco de verdad, contra el config real y en las dos direcciones.

    La segunda mitad importa tanto como la primera: si `hiit_dia_1/<clave>`
    también saliera huérfana, el guion estaría llamando huérfano a medio
    programa y el borrado se llevaría por delante el bloque entero.
    """
    cfg = con_bloque_hiit(cfg)
    claves = ("remo_maquina", "suitcase_carry", "air_bike", "plancha_frontal",
              "wall_ball")
    assert huerfanas(cfg, _filas(*(("dia_1", k) for k in claves))) == sorted(
        ("dia_1", k) for k in claves
    )
    assert huerfanas(cfg, _filas(*(("hiit_dia_1", k) for k in claves))) == []


def test_un_ejercicio_que_esta_en_tres_rutinas_no_sale_huerfano_en_ninguna(cfg):
    """`plancha_lateral` vive en `dia_1`, `dia_2` y `dia_3` a la vez.

    Es el reverso del test de arriba. Un chequeo por par mal escrito -que
    comparase contra los ejercicios de UNA rutina- lo declararía huérfano en
    dos de las tres, y borraría cargas vivas.
    """
    assert huerfanas(cfg, _filas(
        ("dia_1", "plancha_lateral"),
        ("dia_2", "plancha_lateral"),
        ("dia_3", "plancha_lateral"),
    )) == []


def test_una_rutina_entera_que_desaparece_del_yaml_cae_sola(cfg):
    """Sin caso especial: ninguno de sus pares encuentra sitio."""
    assert huerfanas(cfg, _filas(
        ("dia_9", "prensa_horizontal"), ("dia_9", "plancha_lateral")
    )) == [("dia_9", "plancha_lateral"), ("dia_9", "prensa_horizontal")]


def test_la_base_limpia_no_tiene_nada_que_borrar(cfg):
    """Todo lo que el YAML declara está a salvo, ejercicio a ejercicio.

    Este es el test que convierte el guion en algo que se puede lanzar sin
    mirar: recorre el `config.yaml` entero y exige que NINGUNA de sus claves
    salga huérfana. El día que alguien cambie `huerfanas` y se le escape una
    comparación, esto lo dice antes de que el borrado se lo lleve puesto.
    """
    todas = _filas(*(
        (str(rk), str(ex.get("key")))
        for rk, rutina in (cfg.routines or {}).items()
        for ex in ((rutina or {}).get("exercises") or [])
    ))
    assert len(todas) > 30, "el config real tiene ejercicios de sobra"
    assert huerfanas(cfg, todas) == []


# ---------------------------------------------------------------------------
# Lo que se escribe de verdad con --aplicar
# ---------------------------------------------------------------------------


@pytest.fixture
def base_con_racha(tmp_path, monkeypatch):
    """Una base con una carga y una racha por debajo a medias, y su mejor sesión."""
    import json

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db as appdb
    from app.models import Base
    from app.settings import settings

    ruta = tmp_path / "con_racha.db"
    eng = create_engine(f"sqlite:///{ruta}", future=True)
    Base.metadata.create_all(eng)
    monkeypatch.setattr(appdb, "engine", eng)
    monkeypatch.setattr(appdb, "SessionLocal", sessionmaker(bind=eng, future=True))
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{ruta}")

    with appdb.session_scope() as s:
        s.add(
            ExerciseTarget(
                routine_key="dia_1",
                exercise_key="prensa_horizontal",
                current_sets_json=json.dumps(series(90, 100, 120)),
                below_plan_streak=2,
                below_plan_best_kg=100.0,
                below_plan_best_sets_json=json.dumps(series(70, 90, 100)),
            )
        )
    return appdb


def test_fijar_una_carga_borra_tambien_la_mejor_sesion_de_la_racha(base_con_racha):
    """La racha se contaba contra el objetivo que se acaba de sustituir.

    Desde el 25/09/2026 la mejor sesión se guarda con sus series y el peso es
    solo su proyección. Borrar solo el peso dejaba la sesión viva: `load_state`
    la leía y la próxima racha arrancaba con una «mejor» medida contra otra
    cosa.
    """
    from scripts.fijar_carga import main

    assert main(["dia_1", "prensa_horizontal", "70,90,110", "--aplicar"]) == 0

    with base_con_racha.session_scope() as s:
        fila = s.query(ExerciseTarget).one()
        assert fila.below_plan_streak == 0
        assert fila.below_plan_best_kg is None
        assert fila.below_plan_best_sets_json is None, (
            "la mejor sesión de la racha vieja ha sobrevivido a fijar la carga"
        )
