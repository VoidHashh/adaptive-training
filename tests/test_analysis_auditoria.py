"""Vista 4: la auditoría del semáforo, que es la única que juzga al motor.

Lo que más se prueba aquí no son los contadores -sumar es fácil- sino las
distinciones que hacen que un contador signifique algo:

  - un día sin decisión NO es un día verde. Es el fallo que convertiría un mes
    con el PC apagado en un mes estupendo;
  - "nunca disparó" son DOS cosas: la regla que se evaluó ciento ochenta días y
    nunca se cumplió -mal calibrada o sobra- y la que no se pudo evaluar ni una
    vez porque le faltaba un dato -ciega, y recalibrarla no la arreglaría-.
    Juntarlas manda a tocar el umbral equivocado;
  - una regla que dispara a diario y nunca manda no es lo mismo que una que
    manda siempre que dispara;
  - la tabla es append-only, así que un día recalculado tiene varias filas. Solo
    cuenta la vigente, o cada ensayo inflaría los contadores.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis.auditoria import (
    auditoria_reglas,
    dias_de_luz,
    distribucion,
    progresion_ejercicios,
    puertas_de_progresion,
    recalibraciones,
    reglas_especiales,
    vista_auditoria,
)
from app.models import Base, Decision, RuleState
from tests.dobles import doble_de
from app.config_loader import Config

HOY = date(2026, 9, 11)
N = 28


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def dia(i: int) -> date:
    return HOY - timedelta(days=N - 1 - i)


@doble_de(Config)
class Cfg:
    """Un `config.yaml` de mentira con lo justo que mira esta vista."""

    def __init__(self, reglas=None, especiales=(), set_types=None):
        self._reglas = (
            reglas
            if reglas is not None
            else [
                {"name": "lumbar_alto", "light": "red", "description": "Lumbar >= 7"},
                {"name": "cansancio_alto", "light": "amber", "description": "Cansancio >= 7"},
                {"name": "hrv_baja_1d", "light": "amber", "description": "HRV baja"},
            ]
        )
        self.raw = {
            "special_rules": [{"name": n} for n in especiales],
            "set_types": set_types or {},
        }

    def all_rules(self):
        return list(self._reglas)


def decision(
    db,
    i,
    *,
    luz="green",
    determinante=None,
    disparadas=(),
    saltadas=(),
    valores=None,
    fuente="checkin",
    hash_cfg="aaa",
    vigente=True,
    progresion=None,
    sesion=None,
):
    db.add(
        Decision(
            date=dia(i),
            light=luz,
            trigger_rule=determinante,
            fired_rules_json=json.dumps(list(disparadas)),
            skipped_rules_json=json.dumps(list(saltadas)),
            inputs_snapshot_json=json.dumps({"values": valores or {}}),
            config_hash=hash_cfg,
            source=fuente,
            is_current=vigente,
            progression_json=json.dumps(progresion) if progresion else None,
            planned_session_json=json.dumps(sesion) if sesion else None,
        )
    )


def regla_de(filas, nombre):
    return next(f for f in filas if f["nombre"] == nombre)


# ---------------------------------------------------------------------------
# Los días
# ---------------------------------------------------------------------------


def test_cada_dia_trae_su_luz_y_los_deslizadores_que_vio_el_motor(db):
    """Los del SNAPSHOT, no los de la tabla de check-ins.

    Es una auditoría de lo que decidió el motor, así que lo que se pinta encima
    del calendario tiene que ser lo que el motor tenía delante. Un check-in
    rellenado por la tarde, después de una decisión de las nueve, no estuvo en
    esa decisión, y pintarlo encima diría que sí.
    """
    decision(
        db,
        0,
        luz="amber",
        determinante="cansancio_alto",
        disparadas=["cansancio_alto", "hrv_baja_1d"],
        valores={"fatigue": 4, "mood": 2, "hrv": 58.0, "rhr": 51.0},
    )
    db.commit()

    d = dias_de_luz(db, dia(0), dia(0))[0]

    assert d["luz"] == "amber"
    # En minúsculas, y a propósito: el nombre tiene que poder meterse dentro de
    # una frase ("100,0 % ámbar") además de en la insignia de la regla, que de
    # todas formas la pinta en versales enteras por CSS. La versión con
    # mayúscula inicial obligaba a llevar una segunda tabla para la prosa, y la
    # PWA acabó no llevando ninguna: escribía "amber" en la pantalla.
    assert d["nombre_luz"] == "ámbar"
    assert d["regla_determinante"] == "cansancio_alto"
    assert d["reglas_disparadas"] == ["cansancio_alto", "hrv_baja_1d"]
    assert d["fuente"] == "checkin"
    # Solo los deslizadores: hrv y rhr son señales del reloj, no cosas que él
    # conteste, y el calendario superpone lo que contestó.
    assert d["sliders"] == {"fatigue": 4, "mood": 2}


def test_un_dia_sin_decision_es_un_hueco_no_un_verde(db):
    """El fallo que haría bueno un mes con el PC apagado.

    Un día sin fila no es un día tranquilo: es un día en que no hubo semáforo.
    Contarlo como verde -que es lo que pasa si se rellenan los huecos con el
    color por defecto- convertiría el apagón en la mejor racha del histórico.
    """
    decision(db, 0, luz="red", determinante="lumbar_alto", disparadas=["lumbar_alto"])
    decision(db, 3, luz="green")
    db.commit()

    dias = dias_de_luz(db, dia(0), dia(3))

    assert [d["luz"] for d in dias] == ["red", None, None, "green"]
    assert dias[1]["na"] == "no hay decisión guardada para este día"
    assert dias[1]["sliders"] == {}
    assert dias[1]["regla_determinante"] is None


def test_la_distribucion_no_mete_los_huecos_en_el_denominador(db):
    """"60% de días verdes" y "60% de los días que se miraron" no son lo mismo."""
    for i in range(4):
        decision(db, i, luz="green")
    decision(db, 4, luz="red", determinante="lumbar_alto", disparadas=["lumbar_alto"])
    # dia(5) y dia(6) sin decisión.
    db.commit()

    g = distribucion(dias_de_luz(db, dia(0), dia(6)))["global"]

    assert g["n"] == 5
    assert g["sin_decision"] == 2
    assert g["green"] == 4
    assert g["red"] == 1
    assert g["porcentaje"]["green"] == 80.0  # 4 de 5, no 4 de 7
    assert g["porcentaje"]["red"] == 20.0


def test_la_distribucion_por_semana_parte_por_semanas_iso(db):
    """Semanas ISO, que empiezan en lunes, que es como él cuenta las suyas.

    Y las de los extremos salen partidas, que es lo correcto: la ventana son los
    últimos N días, no los últimos N días redondeados al lunes anterior. Una
    semana de dos días tiene que decir que son dos y no fingir que son siete,
    porque si no el porcentaje de esa semana se calcularía contra un denominador
    que nadie ha observado.
    """
    for i in range(N):
        decision(db, i, luz="green" if i % 7 else "amber", determinante=None)
    db.commit()

    d = distribucion(dias_de_luz(db, dia(0), dia(N - 1)))

    assert d["global"]["n"] == N
    # 28 días desde un sábado: dos semanas partidas en los bordes y tres enteras.
    assert [s["n"] for s in d["por_semana"]] == [2, 7, 7, 7, 5]
    assert [s["semana"] for s in d["por_semana"]] == [33, 34, 35, 36, 37]

    for semana in d["por_semana"]:
        assert semana["desde"] <= semana["hasta"]
        # Cada tramo cae entero dentro de la semana ISO que dice ser.
        for extremo in (semana["desde"], semana["hasta"]):
            iso = date.fromisoformat(extremo).isocalendar()
            assert (iso[0], iso[1]) == (semana["anio"], semana["semana"])

    # Nada se pierde ni se cuenta dos veces al repartir por semanas.
    assert sum(s["n"] for s in d["por_semana"]) == d["global"]["n"]


def test_sin_porcentaje_cuando_no_hay_ni_un_dia(db):
    """Cero de cero no es cero por ciento: es que no hay nada que repartir."""
    g = distribucion(dias_de_luz(db, dia(0), dia(3)))["global"]
    assert g["n"] == 0
    assert g["porcentaje"] is None
    assert g["sin_decision"] == 4


# ---------------------------------------------------------------------------
# Las reglas
# ---------------------------------------------------------------------------


def test_los_dos_contadores_van_por_separado(db):
    """Disparar y mandar no es lo mismo, y la diferencia es el dato.

    Una regla que dispara todos los días pero nunca decide nada es ruido con
    nombre: sale en la lista de disparos como si fuera importante, y no ha
    cambiado el color ni una vez.
    """
    for i in range(10):
        decision(
            db,
            i,
            luz="amber",
            determinante="lumbar_alto" if i < 3 else "cansancio_alto",
            disparadas=["hrv_baja_1d", "lumbar_alto" if i < 3 else "cansancio_alto"],
        )
    db.commit()

    filas, _ = auditoria_reglas(Cfg(), dias_de_luz(db, dia(0), dia(9)))

    hrv = regla_de(filas, "hrv_baja_1d")
    assert hrv["veces_disparada"] == 10
    assert hrv["veces_determinante"] == 0
    assert "no mandó en ninguno" in hrv["lectura"]

    lumbar = regla_de(filas, "lumbar_alto")
    assert lumbar["veces_disparada"] == 3
    assert lumbar["veces_determinante"] == 3
    assert lumbar["ultima_vez"] == dia(2).isoformat()


def test_nunca_disparo_y_nunca_se_pudo_evaluar_no_son_lo_mismo(db):
    """La distinción que salva esta vista entera.

    `cansancio_alto` se evaluó diez días y no saltó: o está mal calibrada o
    sobra, y eso lo decide él. `hrv_baja_1d` no se evaluó NI UNA VEZ porque
    faltaba la HRV: no está mal calibrada, está ciega, y moverle el umbral no
    haría absolutamente nada.

    Si las dos salieran bajo el mismo rótulo, la acción obvia -bajar el umbral-
    sería la correcta para una y completamente inútil para la otra.
    """
    for i in range(10):
        decision(
            db,
            i,
            luz="red",
            determinante="lumbar_alto",
            disparadas=["lumbar_alto"],
            saltadas=[{"name": "hrv_baja_1d", "missing": ["hrv"]}],
        )
    db.commit()

    filas, nunca = auditoria_reglas(Cfg(), dias_de_luz(db, dia(0), dia(9)))

    cansancio = regla_de(filas, "cansancio_alto")
    assert cansancio["estado"] == "nunca_disparo"
    assert cansancio["dias_evaluada"] == 10
    assert "o está mal calibrada o sobra" in cansancio["lectura"]

    hrv = regla_de(filas, "hrv_baja_1d")
    assert hrv["estado"] == "nunca_evaluada"
    assert hrv["dias_evaluada"] == 0
    assert hrv["veces_saltada"] == 10
    assert hrv["le_falto"] == {"hrv": 10}
    assert "está ciega" in hrv["lectura"]
    assert "mal calibrada" not in hrv["lectura"]

    # Las dos salen en la lista que se pidió, y cada una con su estado.
    assert {f["nombre"] for f in nunca} == {"cansancio_alto", "hrv_baja_1d"}


def test_una_regla_ciega_sin_motivo_apuntado_lo_dice_en_vez_de_decir_algun_dato(db):
    """"Le falta algún dato" tapaba un fallo del propio registro de faltas.

    Aquí la regla se saltó diez días y ni uno apuntó QUÉ le faltó. Las dos
    situaciones -se sabe qué falta, no se sabe- se arreglan en sitios distintos:
    la primera mirando el reloj o el check-in, la segunda mirando el motor, que
    se está saltando reglas sin dejar constancia de por qué.

    El texto anterior las dejaba con la misma cara, y encima sonaba a la primera,
    que es la que manda a buscar al sitio equivocado.
    """
    for i in range(10):
        decision(
            db,
            i,
            luz="red",
            determinante="lumbar_alto",
            disparadas=["lumbar_alto"],
            saltadas=[{"name": "hrv_baja_1d"}],  # sin `missing`: no consta qué faltó
        )
    db.commit()

    filas, _ = auditoria_reglas(Cfg(), dias_de_luz(db, dia(0), dia(9)))
    hrv = regla_de(filas, "hrv_baja_1d")

    assert hrv["estado"] == "nunca_evaluada"
    assert hrv["veces_saltada"] == 10
    assert hrv["le_falto"] == {}
    assert "el motivo tampoco se ha registrado" in hrv["lectura"]
    assert "algún dato" not in hrv["lectura"]
    # Y sigue diciendo lo que sí se sabe: que está ciega, no descalibrada.
    assert "está ciega" in hrv["lectura"]
    assert "mal calibrada" not in hrv["lectura"]


def test_una_regla_retirada_del_config_no_se_cuenta_como_viva(db):
    """Disparó de verdad, pero ya no existe. Las dos cosas a la vez.

    Sin esto, el total de una regla borrada del YAML seguiría saliendo en la
    tabla como si estuviera vigilando algo.
    """
    for i in range(5):
        decision(
            db,
            i,
            luz="amber",
            determinante="regla_vieja",
            disparadas=["regla_vieja"],
        )
    db.commit()

    filas, nunca = auditoria_reglas(Cfg(), dias_de_luz(db, dia(0), dia(4)))

    vieja = regla_de(filas, "regla_vieja")
    assert vieja["estado"] == "retirada"
    assert vieja["declarada"] is False
    assert vieja["veces_disparada"] == 5
    assert "YA NO está declarada" in vieja["lectura"]
    assert "regla_vieja" not in {f["nombre"] for f in nunca}


def test_sin_ni_un_dia_de_historico_no_se_acusa_a_ninguna_regla(db):
    """Con la base recién estrenada, trece reglas bajo "o sobran" sería mentira.

    No han fallado: no ha habido ocasión. Siguen todas en la tabla con su
    estado, así que no se esconde ninguna; lo que no se hace es acusarlas de un
    defecto que todavía no puede saberse.
    """
    filas, nunca = auditoria_reglas(Cfg(), dias_de_luz(db, dia(0), dia(9)))

    assert nunca == []
    assert len(filas) == 3
    for f in filas:
        assert f["estado"] == "sin_historico"
        assert "no ha llegado a evaluar" in f["lectura"]
        assert "sobra" not in f["lectura"]


def test_solo_cuenta_la_decision_vigente(db):
    """La tabla es append-only: un día recalculado tiene varias filas.

    Contar todas metería en los contadores los ensayos que se descartaron, y la
    regla que disparó en un recálculo que luego se sustituyó saldría como si
    hubiera decidido algo.
    """
    decision(
        db, 0, luz="red", determinante="lumbar_alto", disparadas=["lumbar_alto"],
        vigente=False,
    )
    decision(
        db, 0, luz="green", determinante=None, disparadas=[], vigente=True,
    )
    db.commit()

    dias = dias_de_luz(db, dia(0), dia(0))
    assert len(dias) == 1
    assert dias[0]["luz"] == "green"

    filas, _ = auditoria_reglas(Cfg(), dias)
    assert regla_de(filas, "lumbar_alto")["veces_disparada"] == 0


def test_las_recalibraciones_salen_del_hash_de_cada_decision(db):
    """Cuatro disparos en seis meses pueden ser de tres reglas distintas.

    El YAML entero de cada día no se guarda, así que corregirlo no se puede;
    decir dónde está la costura, sí, y es lo que hace que el número se lea con la
    cabeza puesta.
    """
    for i in range(6):
        decision(db, i, luz="green", hash_cfg="aaa" if i < 3 else "bbb")
    decision(db, 6, luz="green", hash_cfg="ccc")
    db.commit()

    cambios = recalibraciones(dias_de_luz(db, dia(0), dia(6)))

    assert len(cambios) == 2
    assert cambios[0] == {"fecha": dia(3).isoformat(), "de": "aaa", "a": "bbb"}
    assert cambios[1] == {"fecha": dia(6).isoformat(), "de": "bbb", "a": "ccc"}


def test_las_reglas_especiales_se_cuentan_por_activacion_no_por_dia(db):
    """Una retirada de catorce días es UN suceso, no catorce.

    Contarla por días la pondría catorce veces por delante de una regla que
    saltó tres veces en tres meses, y se leería como que es catorce veces más
    importante.
    """
    db.add(
        RuleState(
            rule_name="retirada_peso_muerto",
            entity="peso_muerto_smith",
            active_from=dia(2),
            active_until=dia(16),
            reason="molestia lumbar dos días seguidos",
        )
    )
    db.commit()

    filas = reglas_especiales(
        db, Cfg(especiales=["retirada_peso_muerto", "semana_de_descarga"]), dia(0), dia(N - 1)
    )

    retirada = regla_de(filas, "retirada_peso_muerto")
    assert retirada["veces_activada"] == 1
    assert retirada["activaciones"][0]["entidad"] == "peso_muerto_smith"
    assert retirada["activaciones"][0]["motivo"] == "molestia lumbar dos días seguidos"

    descarga = regla_de(filas, "semana_de_descarga")
    assert descarga["veces_activada"] == 0
    assert descarga["declarada"] is True
    assert "no se ha activado" in descarga["lectura"]


def test_una_regla_especial_sin_fin_sigue_contando(db):
    """`active_until` a nulo es "sigue activa", no "terminó hace tiempo".

    Con el filtro ingenuo -que exige que termine después del principio de la
    ventana- una retirada indefinida desaparecería de la auditoría justo
    mientras está en vigor.
    """
    db.add(
        RuleState(
            rule_name="retirada_peso_muerto",
            entity="*",
            active_from=dia(1),
            active_until=None,
        )
    )
    db.commit()

    filas = reglas_especiales(db, Cfg(especiales=["retirada_peso_muerto"]), dia(0), dia(N - 1))
    assert regla_de(filas, "retirada_peso_muerto")["veces_activada"] == 1
    assert regla_de(filas, "retirada_peso_muerto")["activaciones"][0]["hasta"] is None


# ---------------------------------------------------------------------------
# La progresión
# ---------------------------------------------------------------------------


SESION = {
    "routine": "dia_1",
    "exercises": [
        {
            "key": "hip_thrust_barra",
            "sets": [
                {"reps": 12, "weight_kg": 100.0},
                {"reps": 12, "weight_kg": 100.0},
                {"reps": 10, "weight_kg": 105.0},
            ],
        }
    ],
}


def test_la_progresion_marca_cuando_subio_y_por_que(db):
    """El porqué sale del registro del motor, no de comparar días consecutivos.

    Una carga que baja y vuelve a subir puede ser un ámbar, una semana de
    descarga o un cambio a mano. Adivinar cuál de las tres fue es exactamente
    como se fabrica una auditoría que se inventa la historia.
    """
    decision(db, 0, luz="green", sesion=SESION)
    decision(
        db,
        1,
        luz="green",
        sesion=SESION,
        progresion={
            "routine": "dia_1",
            "gate_open": True,
            "sets_allowed": True,
            "reps_allowed": True,
            "exercises": [
                {
                    "key": "hip_thrust_barra",
                    "changed": True,
                    "mode": "load",
                    "what": "100 -> 105 kg en la serie más pesada",
                    "why": "tres sesiones limpias seguidas",
                }
            ],
        },
    )
    db.commit()

    filas = progresion_ejercicios(db, Cfg(), dia(0), dia(1))
    f = next(x for x in filas if x["ejercicio"] == "hip_thrust_barra")

    assert f["rutina"] == "dia_1"
    assert f["dias_prescrito"] == 2
    assert f["puntos"][0]["peso_max_kg"] == 105.0
    assert f["puntos"][0]["series_totales"] == 3
    assert len(f["subidas"]) == 1
    assert f["subidas"][0]["que"] == "100 -> 105 kg en la serie más pesada"
    assert f["subidas"][0]["por_que"] == "tres sesiones limpias seguidas"
    assert "1 subida;" in f["lectura"]


def test_cada_freno_sale_con_su_motivo_porque_la_accion_es_distinta(db):
    """Un techo se arregla cambiando el ejercicio; un dato que falta, apuntándolo.

    Mandar "toca cambiar el ejercicio" cuando lo único que pasa es que no
    apuntaste la carga es peor que no decir nada.
    """
    casos = [
        ({"at_ceiling": True}, "techo"),
        ({"needs_data": True}, "falta_dato"),
        ({"blocked_by": "volumen bloqueado: ámbar"}, "puerta"),
        ({"waiting": "faltan 2 sesiones limpias"}, "racha"),
    ]
    for i, (campos, _) in enumerate(casos):
        decision(
            db,
            i,
            luz="green",
            sesion=SESION,
            progresion={
                "routine": "dia_1",
                "gate_open": True,
                "sets_allowed": True,
                "reps_allowed": True,
                "exercises": [{"key": "hip_thrust_barra", "changed": False, **campos}],
            },
        )
    db.commit()

    f = progresion_ejercicios(db, Cfg(), dia(0), dia(3))[0]

    assert f["subidas"] == []
    assert [x["motivo"] for x in f["frenos"]] == [m for _, m in casos]
    assert "techo alcanzado" in f["frenos"][0]["por_que"]
    assert "apuntada en Hevy" in f["frenos"][1]["por_que"]
    assert f["frenos"][2]["por_que"] == "volumen bloqueado: ámbar"


def test_un_ejercicio_que_lleva_meses_parado_lo_dice_con_su_motivo(db):
    """Doce semanas sin avanzar y el mensaje de la mañana no lo mencionó nunca.

    Ese fue el fallo real. Un ejercicio parado es información accionable, y en
    esta vista tiene que salir dicho con todas las letras.
    """
    for i in range(N):
        decision(
            db,
            i,
            luz="green",
            sesion=SESION,
            progresion={
                "routine": "dia_1",
                "gate_open": True,
                "sets_allowed": True,
                "reps_allowed": True,
                "exercises": [
                    {"key": "hip_thrust_barra", "changed": False, "at_ceiling": True}
                ],
            },
        )
    db.commit()

    f = progresion_ejercicios(db, Cfg(), dia(0), dia(N - 1))[0]

    assert f["subidas"] == []
    assert "NO ha subido ni una vez" in f["lectura"]
    assert "está en su techo" in f["lectura"]
    assert f"{N} días" in f["lectura"]


def test_un_parado_sin_motivo_apuntado_se_llama_hueco_del_registro(db):
    """Si no subió y no hay porqué, lo que falta es el registro, no la explicación.

    Inventar aquí un motivo plausible sería lo peor que puede hacer una
    auditoría: taparía el único sitio donde se ve que el motor dejó de apuntar.
    """
    decision(
        db,
        0,
        luz="green",
        sesion=SESION,
        progresion={
            "routine": "dia_1",
            "gate_open": True,
            "sets_allowed": True,
            "reps_allowed": True,
            "exercises": [{"key": "hip_thrust_barra", "changed": False}],
        },
    )
    db.commit()

    f = progresion_ejercicios(db, Cfg(), dia(0), dia(0))[0]
    assert f["frenos"] == []
    assert "hueco del registro" in f["lectura"]


def test_una_puerta_cerrada_es_un_suceso_del_dia_no_uno_por_ejercicio(db):
    """Doce fichas con doce frenos parecen doce problemas; son uno.

    El motor le pone el motivo de la puerta a cada ejercicio por separado, así
    que en las fichas ya sale. Lo que no se ve ahí es que ese día no subió
    NINGUNO y por lo mismo.
    """
    decision(
        db,
        0,
        luz="amber",
        sesion=SESION,
        progresion={
            "routine": "dia_1",
            "gate_open": False,
            "gate_reason": "día ámbar: no se progresa",
            "sets_allowed": False,
            "sets_reason": "día ámbar: no se progresa",
            "reps_allowed": False,
            "reps_reason": "día ámbar: no se progresa",
            "exercises": [
                {"key": "a", "changed": False, "blocked_by": "día ámbar: no se progresa"},
                {"key": "b", "changed": False, "blocked_by": "día ámbar: no se progresa"},
            ],
        },
    )
    decision(
        db,
        1,
        luz="green",
        sesion=SESION,
        progresion={
            "routine": "dia_1",
            "gate_open": True,
            "sets_allowed": True,
            "reps_allowed": True,
            "exercises": [],
        },
    )
    db.commit()

    puertas = puertas_de_progresion(db, dia(0), dia(1))

    assert len(puertas) == 1
    assert puertas[0]["fecha"] == dia(0).isoformat()
    assert puertas[0]["luz"] == "amber"
    assert puertas[0]["puerta_abierta"] is False
    assert puertas[0]["motivo"] == "día ámbar: no se progresa"


def test_las_dos_puertas_van_separadas_porque_lo_son_en_el_motor(db):
    """Añadir una serie efectiva no es el mismo riesgo que sumar dos reps.

    Con un solo permiso había que elegir entre ser laxo con lo primero o
    asfixiar lo segundo, y el motor las separó por eso. La auditoría tiene que
    enseñarlas igual de separadas o no se puede ver cuál de las dos frena.
    """
    decision(
        db,
        0,
        luz="green",
        sesion=SESION,
        progresion={
            "routine": "dia_1",
            "gate_open": True,
            "gate_reason": "",
            "sets_allowed": False,
            "sets_reason": "volumen al tope semanal",
            "reps_allowed": True,
            "reps_reason": "",
            "exercises": [],
        },
    )
    db.commit()

    p = puertas_de_progresion(db, dia(0), dia(0))[0]
    assert p["puerta_abierta"] is True
    assert p["series_permitidas"] is False
    assert p["motivo_series"] == "volumen al tope semanal"
    assert p["reps_permitidas"] is True


def test_las_series_efectivas_no_cuentan_el_calentamiento(db):
    """El volumen que cuenta es el que estresa, y la primera serie floja no lo es."""
    sesion = {
        "routine": "dia_1",
        "exercises": [
            {
                "key": "hip_thrust_barra",
                "sets": [
                    {"reps": 15, "weight_kg": 40.0, "type": "warmup"},
                    {"reps": 12, "weight_kg": 100.0},
                    {"reps": 12, "weight_kg": 100.0},
                ],
            }
        ],
    }
    decision(db, 0, luz="green", sesion=sesion)
    db.commit()

    cfg = Cfg(set_types={"warmup_markers": ["warmup"]})
    p = progresion_ejercicios(db, cfg, dia(0), dia(0))[0]["puntos"][0]

    assert p["series_totales"] == 3
    assert p["series_efectivas"] == 2
    assert p["peso_max_kg"] == 100.0


# ---------------------------------------------------------------------------
# La vista entera
# ---------------------------------------------------------------------------


def test_la_vista_entera_sale_con_el_config_de_verdad(db):
    """Contra el `config.yaml` real, que es el que tiene que poder auditar.

    Un doble de test puede tener tres reglas bien formadas; el archivo de verdad
    tiene todas las del semáforo y tres especiales, y es donde aparecería un
    `name` repetido o un bloque que la vista no sabe leer.

    Aquí había un `== 13` escrito a mano, y al borrar `resaca_finde` este test
    se puso rojo por la única razón por la que no debería: porque el número de
    reglas había cambiado a propósito. Un test que hay que ir a actualizar cada
    vez que se toca el YAML enseña a actualizarlo sin mirar.

    Lo que de verdad importaba comprobar -y el contador no comprobaba- es que la
    vista vea EXACTAMENTE las reglas que hay en el fichero, ni una de menos. Una
    regla que se evalúa cada mañana y no sale en la auditoría es una regla que
    decide sin que se pueda revisar, y esa es la única forma de que se quede mal
    calibrada para siempre.
    """
    from app.config_loader import load_config

    cfg = load_config("config.yaml")
    for i in range(N):
        decision(db, i, luz="green" if i % 3 else "amber", determinante=None)
    db.commit()

    v = vista_auditoria(db, cfg, dias=N, hoy=HOY)

    en_el_yaml = {
        r["name"]
        for luz in ("red", "amber")
        for r in cfg.raw["thresholds"].get(luz) or []
    }
    assert v["vista"] == "auditoria"
    assert v["ventana"]["dias"] == N
    assert len(v["dias"]) == N
    assert v["distribucion"]["global"]["n"] == N
    assert {r["nombre"] for r in v["reglas"]} == en_el_yaml
    assert len(v["reglas"]) == len(en_el_yaml), "alguna sale dos veces"
    # Ninguna disparó, pero TODAS se evaluaron: es el caso "o mal calibradas o
    # sobran" en estado puro, y tiene que salir dicho así.
    assert len(v["nunca_dispararon"]) == len(en_el_yaml)
    assert all(f["estado"] == "nunca_disparo" for f in v["nunca_dispararon"])
    assert {r["nombre"] for r in v["reglas_especiales"]} == {
        "retirada_peso_muerto",
        "descarga_press_hombro",
        "semana_de_descarga",
    }
    assert v["recalibraciones"] == []
    assert v["progresion"] == []


def test_una_lista_vacia_nunca_se_queda_sin_explicar_por_que_lo_esta(db):
    """Y la explicación cambia según QUÉ falte, aunque la lista salga igual.

    Con veintiocho días decididos, tres secciones salen a cero: no hubo
    recalibraciones, la puerta no se cerró y no se guardó ninguna sesión. Las dos
    primeras son buenas noticias y hay que decirlo con ese número delante, porque
    "no se cerró ni uno de los 28 días" y "no se cerró" no son la misma frase.
    """
    for i in range(N):
        decision(db, i, luz="green", determinante=None)
    db.commit()

    lect = vista_auditoria(db, Cfg(), dias=N, hoy=HOY)["lecturas"]

    assert f"{N} días con decisión" in lect["recalibraciones"]
    assert "ninguna recalibración" in lect["recalibraciones"]
    assert f"{N} días con decisión" in lect["puertas_cerradas"]
    assert "no ha frenado la progresión" in lect["puertas_cerradas"]
    assert "hueco del registro" in lect["progresion"]


def test_sin_una_sola_decision_las_tres_lecturas_culpan_al_motor_parado(db):
    """El eslabón de más abajo otra vez, y aquí invierte el signo de la noticia.

    Una lista de puertas vacía se lee "el semáforo no ha frenado nada", que es
    tranquilizador. Con la base recién estrenada la misma lista vacía significa
    lo contrario: no se ha evaluado ni una vez. Escribir la frase optimista aquí
    sería certificar que funciona bien un sistema que no ha arrancado.
    """
    v = vista_auditoria(db, Cfg(), dias=N, hoy=HOY)

    assert v["recalibraciones"] == []
    assert v["puertas_cerradas"] == []
    assert v["progresion"] == []

    for seccion in ("recalibraciones", "puertas_cerradas", "progresion"):
        frase = v["lecturas"][seccion]
        assert "no ha llegado a ejecutarse" in frase, seccion
        # Y NINGUNA de las tres se atreve con la lectura buena.
        assert "no ha frenado" not in frase, seccion
        assert "hueco del registro" not in frase, seccion


def test_la_seccion_que_trae_datos_no_lleva_lectura(db):
    """La frase es para el hueco. Con contenido delante, sobra y estorba."""
    decision(db, 0, luz="green", hash_cfg="h1")
    decision(db, 1, luz="green", hash_cfg="h2")  # una recalibración de verdad
    db.commit()

    v = vista_auditoria(db, Cfg(), dias=N, hoy=HOY)

    assert v["recalibraciones"], "el cambio de hash tiene que salir"
    assert v["lecturas"]["recalibraciones"] is None
    # Las otras dos siguen vacías y siguen explicadas.
    assert v["lecturas"]["puertas_cerradas"] is not None


def test_la_ventana_y_la_cobertura_viajan_tambien_aqui(db):
    v = vista_auditoria(db, Cfg(), dias=N, hoy=HOY)
    assert v["ventana"] == {
        "desde": dia(0).isoformat(),
        "hasta": HOY.isoformat(),
        "dias": N,
    }
    assert "cobertura" in v
    assert len(v["dias"]) == N
