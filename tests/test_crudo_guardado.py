"""Lo que las APIs contestan y este sistema tiraba.

Tercera tanda del mismo fallo. Las otras columnas muertas -la sección de
umbrales del fin de semana- se conectaron y el sistema empezó a decidir mejor
desde ese día. Aquí lo que se perdía era HISTÓRICO.

Se escribió que "Garmin no sirve el sueño ni el body battery de hace tres
meses". Medido después (`scripts/sondeo_wellness.py`), es falso a medias: el
sueño vuelve hasta -175 días y el body battery hasta unos -120. Lo que Hevy no
da es el detalle de una sesión más allá de lo que uno se descargue, y eso sí es
definitivo. La parte de Garmin se recupera con `app/backfill.py`; la de Hevy,
no. Guardar el crudo cada día sigue siendo lo correcto por la segunda mitad, y
porque ni el sueño de -175 días trae ya todos los campos que traía el día uno.

`readiness` fue la tercera columna muerta de esa tanda y la única que no se
arregló al conectarla: el cable llevaba a una toma sin corriente. Ver
`app/integrations/garmin.py`.

LAS TRES COLUMNAS, Y QUÉ SE HA HECHO CON CADA UNA
-------------------------------------------------
`models.py` abre con un principio: "todo lo que viene de una API externa se
guarda además en crudo". Las tres columnas `raw_json` que lo implementaban
estaban declaradas y NINGUNA se escribía.

  - `daily_metrics.raw_json` -> CONECTADA. Cuatro llamadas a Garmin por día
    (cinco si se enciende readiness) para quedarse con cinco números y tirar el
    resto, sin caché ninguna detrás.
  - `workout_log.raw_json` -> CONECTADA, y con ella `duration_s`, `total_sets`
    y `total_volume_kg`, que estaban igual de vacías. Esta era la peor de las
    tres: `docs/analisis.md` la CERTIFICABA como resuelta, con un "Sí" en la
    tabla. El documento que tenía que avisar del hueco decía que no lo había.
  - `activities.raw_json` -> BORRADA. Es la única que sobraba de verdad: el
    crudo de cada salida está entero en `data/cache/activities.json`, que se
    fusiona y nunca se poda. Una columna vacía que parece una copia de
    seguridad es peor que no tenerla.

LO QUE VIGILA ESTE FICHERO
--------------------------
  1. El crudo llega desde la API hasta la fila, y sobrevive lo que NO se
     extrae: si solo sobrevivieran los seis números de siempre, guardar el
     crudo no serviría para nada.
  2. La poda es por FORMA y no por nombre. Una lista de campos conocidos
     dejaría de reconocer el día que Garmin renombre uno, sin fallar.
  3. Una pasada sin crudo no borra el crudo bueno: la misma regla que ya
     gobierna los seis escalares.
  4. Cero y "no se sabe" no son el mismo dato.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.engine.signals import DayMetrics
from app.integrations import garmin
from app.integrations.hevy import workout_totals
from app.models import Activity, Base, DailyMetrics, WorkoutLog
from app.repository import (
    CLAVE_PODAS,
    MAX_ELEMENTOS_LISTA,
    crudo_para_guardar,
    podar_crudo,
    upsert_daily_metrics,
)

from tests.conftest import LUNES
from tests.test_runner import (
    HevyFalso,
    TelegramFalso,
    _entrenamiento_completo,
    _plan_guardado,
    corre,
)

DIA = date(2026, 9, 7)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class ApiConDetalle:
    """Garmin contestando de verdad: seis números útiles y un montón más.

    Los campos de más no son adorno. `deepSleepSeconds` y `avgOvernightHrv` son
    ejemplos reales de lo que se descartaba: escalares, pequeños, y justo el
    tipo de dato por el que se querría volver a mirar el histórico.
    """

    def get_hrv_data(self, *_):
        return {
            "hrvSummary": {"lastNightAvg": 60, "lastNight5MinHigh": 88, "status": "BALANCED"},
            "hrvReadings": [{"readingTimeGMT": f"t{i}", "hrvValue": 50 + i} for i in range(240)],
        }

    def get_stats(self, *_):
        return {"restingHeartRate": 52, "minHeartRate": 44, "totalSteps": 8400}

    def get_sleep_data(self, *_):
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": 25200,
                "deepSleepSeconds": 5400,
                "remSleepSeconds": 4800,
                "avgOvernightHrv": 61.5,
                "sleepScores": {"overall": {"value": 80}, "deep": {"qualifierKey": "GOOD"}},
            },
            "sleepMovement": [{"startGMT": f"t{i}", "activityLevel": i % 7} for i in range(480)],
            "wellnessEpochRespirationDataDTOList": [
                {"startTimeGMT": f"t{i}", "respirationValue": 13.0} for i in range(300)
            ],
        }

    def get_body_battery(self, *_):
        return [
            {
                "charged": 55,
                "drained": 61,
                "bodyBatteryValuesArray": [[i, 70 - i // 20] for i in range(1440)],
            }
        ]

    def get_training_readiness(self, *_):
        return [{"score": 74, "level": "HIGH", "sleepScoreFactorPercent": 35}]


def cliente(api=None):
    # `fetch_readiness=True` aquí a propósito, aunque en producción vaya
    # apagada: lo que vigila este fichero es que el crudo de CADA llamada que se
    # hace llegue entero a la fila, y con la quinta apagada la quinta respuesta
    # no existiría y el caso dejaría de cubrirse. El día que se vuelva a
    # encender -otro reloj- estas pruebas ya están escritas.
    c = garmin.GarminClient(
        email="a@b.c", password="x", token_dir="/tmp", fetch_readiness=True
    )
    c._api = api or ApiConDetalle()
    return c


def guardado(db, m: DayMetrics) -> dict:
    """El JSON tal y como ha quedado en la columna, ya parseado."""
    upsert_daily_metrics(db, [m])
    fila = db.scalars(select(DailyMetrics).where(DailyMetrics.date == m.date)).first()
    assert fila is not None and fila.raw_json, "no se ha guardado nada"
    return json.loads(fila.raw_json)


# ---------------------------------------------------------------------------
# 1. El crudo llega desde Garmin hasta la fila
# ---------------------------------------------------------------------------


def test_las_cinco_respuestas_viajan_en_DayMetrics():
    m = cliente().day_metrics(DIA)
    assert set(m.raw) == {"hrv", "stats", "sleep", "body_battery", "readiness"}
    # Y los seis números siguen saliendo de donde salían.
    assert (m.hrv, m.rhr, m.sleep_min, m.sleep_score) == (60.0, 52.0, 420, 80)


def test_sobrevive_lo_que_el_motor_NO_extrae(db):
    """La prueba de que guardar el crudo sirve para algo.

    Si de las cinco respuestas solo sobrevivieran los seis escalares que el
    motor lee, esta columna sería una copia cara de las otras seis columnas.
    Lo que tiene que estar es justo lo que hoy no se mira.
    """
    datos = guardado(db, cliente().day_metrics(DIA))
    dto = datos["sleep"]["dailySleepDTO"]
    assert dto["deepSleepSeconds"] == 5400
    assert dto["avgOvernightHrv"] == 61.5
    assert datos["stats"]["totalSteps"] == 8400
    assert datos["body_battery"][0]["drained"] == 61
    assert datos["readiness"][0]["sleepScoreFactorPercent"] == 35


def test_una_llamada_que_falla_no_deja_clave():
    """"No contestó" y "contestó vacío" no son el mismo dato.

    Que la clave exista es la prueba de que la llamada llegó a hacerse. Si una
    respuesta fallida dejara `{}`, dentro de tres meses no habría forma de
    distinguir una noche sin reloj de un 500 de Garmin.
    """
    api = ApiConDetalle()
    api.get_sleep_data = lambda *_: (_ for _ in ()).throw(RuntimeError("500"))
    m = cliente(api).day_metrics(DIA)
    assert "sleep" not in m.raw
    assert set(m.raw) == {"hrv", "stats", "body_battery", "readiness"}


def test_una_llamada_que_contesta_vacio_si_deja_clave():
    api = ApiConDetalle()
    api.get_stats = lambda *_: {}
    m = cliente(api).day_metrics(DIA)
    assert m.raw["stats"] == {}


def test_un_dia_entero_sin_API_no_inventa_un_crudo_vacio():
    """`raw=None`, no `raw={}`. Un dict vacío se guardaría como '{}' y ocuparía
    una fila que dice haber archivado algo."""
    assert DayMetrics(date=DIA).raw is None
    assert crudo_para_guardar({}) is None
    assert crudo_para_guardar(None) is None


# ---------------------------------------------------------------------------
# 2. La regla de siempre: un `None` nuevo no pisa un dato viejo
# ---------------------------------------------------------------------------


def test_una_segunda_pasada_sin_crudo_no_borra_el_bueno(db):
    """La ventana se relee cada mañana. Un 429 en la segunda pasada no puede
    llevarse por delante lo que se archivó en la primera."""
    guardado(db, cliente().day_metrics(DIA))
    upsert_daily_metrics(db, [DayMetrics(date=DIA, hrv=61.0)])

    fila = db.scalars(select(DailyMetrics).where(DailyMetrics.date == DIA)).first()
    assert fila.raw_json, "la segunda pasada ha borrado el crudo de la primera"
    assert json.loads(fila.raw_json)["stats"]["totalSteps"] == 8400


def test_una_segunda_pasada_con_crudo_si_lo_actualiza(db):
    guardado(db, cliente().day_metrics(DIA))
    upsert_daily_metrics(db, [DayMetrics(date=DIA, raw={"stats": {"totalSteps": 99}})])

    fila = db.scalars(select(DailyMetrics).where(DailyMetrics.date == DIA)).first()
    assert json.loads(fila.raw_json)["stats"]["totalSteps"] == 99


def test_el_crudo_no_entra_en_la_igualdad_de_dos_filas():
    """`DayMetrics` es una dataclass congelada: si el crudo entrase en el
    `__eq__` entraría también en el `__hash__`, y un dict no es hasheable."""
    a = DayMetrics(date=DIA, hrv=60.0, raw={"stats": {}})
    b = DayMetrics(date=DIA, hrv=60.0)
    assert a == b
    assert len({a, b}) == 1


# ---------------------------------------------------------------------------
# 3. La poda es por forma, no por nombre
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nombre",
    [
        "sleepMovement",
        "bodyBatteryValuesArray",
        # Los dos de arriba son los de hoy. Los de abajo son el motivo de podar
        # por forma: el día que Garmin renombre uno, una lista de nombres
        # conocidos dejaría de reconocerlo y no fallaría nada.
        "sleepMovementV2",
        "un_nombre_que_no_existe_todavia",
    ],
)
def test_una_serie_larga_se_poda_se_llame_como_se_llame(nombre):
    podado, cortes = podar_crudo({nombre: list(range(500))})
    assert podado[nombre]["__podado__"] == 500
    assert cortes == [f"{nombre}: 500 elementos"]


def test_una_lista_corta_se_queda_entera():
    corta = list(range(MAX_ELEMENTOS_LISTA))
    podado, cortes = podar_crudo({"zonas": corta})
    assert podado["zonas"] == corta
    assert cortes == []


def test_los_escalares_sobreviven_a_cualquier_profundidad():
    """Es la mitad que justifica podar. Lo que haría falta dentro de cuatro
    semanas -fases de sueño, respiración media, mínimos- son escalares."""
    crudo = {"a": {"b": {"c": {"d": 7, "e": [1, 2, 3]}}}, "f": "texto", "g": None}
    podado, cortes = podar_crudo(crudo)
    assert podado == crudo
    assert cortes == []


def test_de_lo_podado_queda_el_recuento_y_una_muestra():
    """La muestra dice la FORMA de lo que se cortó.

    Sin ella, dentro de tres meses "aquí había 1440 cosas" no basta para decidir
    si merece la pena volver a guardarlo entero. Con dos elementos delante se ve
    que eran pares [instante, valor] y se sabe qué se está perdiendo.
    """
    serie = [[i, 70 - i] for i in range(1440)]
    podado, _ = podar_crudo({"bodyBatteryValuesArray": serie})
    cortado = podado["bodyBatteryValuesArray"]
    assert cortado["__podado__"] == 1440
    assert cortado["__muestra__"] == [[0, 70], [1, 69]]


def test_la_anotacion_de_la_poda_dice_la_ruta_entera():
    _, cortes = podar_crudo({"sleep": {"dto": {"movimiento": list(range(90))}}})
    assert cortes == ["sleep.dto.movimiento: 90 elementos"]


def test_lo_podado_queda_anotado_dentro_del_propio_json(db):
    """Un recorte que no se anota es un dato perdido sin rastro."""
    datos = guardado(db, cliente().day_metrics(DIA))
    podas = " | ".join(datos[CLAVE_PODAS])
    for esperado in ("hrvReadings", "sleepMovement", "bodyBatteryValuesArray"):
        assert esperado in podas, f"{esperado} se podó y no se dijo"
    assert "1440 elementos" in podas


def test_sin_nada_que_podar_no_se_ensucia_el_json():
    datos = json.loads(crudo_para_guardar({"stats": {"restingHeartRate": 52}}))
    assert CLAVE_PODAS not in datos


def test_una_fila_enorme_despues_de_podar_se_avisa_pero_se_guarda(caplog):
    """Que quede grande DESPUÉS de podar significa que la respuesta ha cambiado
    de forma. No se recorta por tamaño -eso sería tirar datos por un criterio
    que no significa nada-, pero hay que verlo antes de que sean 190 filas."""
    gordo = {f"campo_{i}": "x" * 500 for i in range(500)}
    with caplog.at_level("WARNING"):
        texto = crudo_para_guardar(gordo, etiqueta="wellness")
    assert json.loads(texto)["campo_0"] == "x" * 500, "se ha guardado igual"
    assert any("cambiado de forma" in r.getMessage() for r in caplog.records)


def test_una_noche_entera_de_garmin_cabe_en_una_fila(db):
    """El número que decide si esto se puede tener encendido en casa.

    Medido con esta respuesta: 61 KB al día sin podar, 1,2 KB podada. Sobre los
    190 días de la ventana de histórico son 11 MB contra 232 KB, en un servidor
    de casa y en una tabla que se va a consultar desde el móvil. Y no se pierde
    un solo escalar: lo que se va son cuatro series por minuto.
    """
    fila_json = crudo_para_guardar(cliente().day_metrics(DIA).raw)
    assert len(fila_json) < 5000, f"{len(fila_json)} caracteres por día es mucho"
    assert json.loads(fila_json)["sleep"]["dailySleepDTO"]["deepSleepSeconds"] == 5400


# ---------------------------------------------------------------------------
# 4. El entrenamiento: tres números y el detalle de cada serie
# ---------------------------------------------------------------------------


def entreno(sets: list[dict], **extra) -> dict:
    w = {
        "id": "w9",
        "start_time": "2026-09-07T18:00:00Z",
        "end_time": "2026-09-07T19:05:00Z",
        "exercises": [{"exercise_template_id": "abc", "sets": sets}],
    }
    w.update(extra)
    return w


def test_los_tres_numeros_de_un_entrenamiento():
    t = workout_totals(
        entreno([
            {"type": "normal", "weight_kg": 60.0, "reps": 10},
            {"type": "normal", "weight_kg": 60.0, "reps": 8},
        ])
    )
    assert t.duration_s == 3900
    assert t.total_sets == 2
    assert t.total_volume_kg == pytest.approx(1080.0)


def test_el_calentamiento_cuenta_y_es_a_proposito():
    """Excluirlo daría un escalón en la gráfica el día del despliegue.

    Desde que el motor marca las series de calentamiento en Hevy, la proporción
    de series marcadas cambia por un cambio de CÓDIGO, no de entrenamiento. Un
    total que las excluyera saltaría ese día sin que hubiera pasado nada.
    """
    con_warmup = workout_totals(
        entreno([
            {"type": "warmup", "weight_kg": 20.0, "reps": 10},
            {"type": "normal", "weight_kg": 60.0, "reps": 10},
        ])
    )
    assert con_warmup.total_sets == 2
    assert con_warmup.total_volume_kg == pytest.approx(800.0)


def test_una_serie_sin_peso_suma_serie_pero_no_kilos():
    t = workout_totals(
        entreno([
            {"type": "normal", "duration_seconds": 45},
            {"type": "normal", "weight_kg": 40.0, "reps": 12},
        ])
    )
    assert t.total_sets == 2
    assert t.total_volume_kg == pytest.approx(480.0)


def test_un_entrenamiento_sin_ejercicios_no_da_cero_sino_nada():
    """Cero series es un dato; "no se pudo leer" es otro. En una gráfica de
    volumen, un cero falso es un día de descanso que no existió."""
    t = workout_totals({"id": "w0", "exercises": []})
    assert t.total_sets is None and t.total_volume_kg is None


@pytest.mark.parametrize(
    "inicio,fin",
    [
        ("2026-09-07T19:00:00Z", "2026-09-07T18:00:00Z"),  # al revés
        ("2026-09-07T18:00:00Z", "2026-09-08T18:00:00Z"),  # 24 h de sesión
    ],
)
def test_una_duracion_imposible_se_queda_sin_dato(inicio, fin):
    """Guardar una duración negativa la da por buena para siempre."""
    w = entreno([{"weight_kg": 1, "reps": 1}], start_time=inicio, end_time=fin)
    assert workout_totals(w).duration_s is None


def test_sin_hora_de_fin_no_se_inventa_la_duracion():
    w = entreno([{"weight_kg": 1, "reps": 1}])
    del w["end_time"]
    assert workout_totals(w).duration_s is None
    assert workout_totals(w).total_sets == 1, "lo demás sí se cuenta"


def test_reconciliar_rellena_las_cuatro_columnas(db, cfg):
    """De punta a punta: la noche escribe lo que la noche leía y tiraba."""
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    w = _entrenamiento_completo(_plan_guardado(db))
    for ex in w["exercises"]:
        for s in ex["sets"]:
            s["weight_kg"] = 50.0
    w["end_time"] = f"{LUNES.isoformat()}T19:00:00Z"

    from app.runner import run_reconcile

    run_reconcile(db, cfg, LUNES, workouts=[w])

    fila = db.scalars(select(WorkoutLog)).first()
    assert fila is not None
    assert fila.duration_s == 3600
    assert fila.total_sets and fila.total_sets > 0
    assert fila.total_volume_kg and fila.total_volume_kg > 0
    assert fila.raw_json, "el entrenamiento se leyó, se usó y se volvió a tirar"


def test_el_peso_de_cada_serie_solo_sobrevive_en_el_crudo(db, cfg):
    """El motivo de la columna, y no es el mismo que en wellness.

    El estado del motor guarda el peso OBJETIVO y si se alcanzó; lo que de
    verdad se levantó en cada serie no lo guarda nadie más. Sin esto, "¿cuánto
    subió el hip thrust en tres meses?" no tiene respuesta posible.
    """
    corre(db, cfg, hevy=HevyFalso(), tg=TelegramFalso())
    w = _entrenamiento_completo(_plan_guardado(db))
    w["exercises"][0]["sets"][0]["weight_kg"] = 67.5

    from app.runner import run_reconcile

    run_reconcile(db, cfg, LUNES, workouts=[w])

    fila = db.scalars(select(WorkoutLog)).first()
    assert "67.5" in fila.raw_json


# ---------------------------------------------------------------------------
# 5. La columna que sobraba
# ---------------------------------------------------------------------------


def test_activities_ya_no_declara_una_copia_que_no_hace():
    """La única de las tres que se borra en vez de conectarse.

    El crudo de cada salida está entero en `data/cache/activities.json`, que se
    fusiona en cada refresco y nunca se poda. Una segunda copia en la base solo
    añadía peso; una segunda copia VACÍA añadía además la impresión de que el
    crudo estaba a salvo en dos sitios cuando no lo estaba en ninguno.
    """
    assert "raw_json" not in Activity.__table__.columns


def test_las_otras_dos_si_la_declaran():
    assert "raw_json" in DailyMetrics.__table__.columns
    assert "raw_json" in WorkoutLog.__table__.columns
