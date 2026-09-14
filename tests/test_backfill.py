"""Que los días que el sistema no estuvo vivo dejen de ser invisibles.

`daily_metrics` solo se escribía dentro de `run_daily`. Cada día que el proceso
no corrió -el PC apagado, el contenedor parado, un despliegue a media mañana- no
tiene fila y no la iba a tener nunca. Y el agujero no se ve: en la base de datos
un día ausente y un día sin reloj son exactamente la misma nada.

Lo que se prueba aquí no es "el backfill escribe filas". Es la parte que decide
si el backfill sirve o hace daño:

  - qué se considera PENDIENTE, que es donde vive la diferencia entre "no había
    dato" y "no se pudo leer". Confundirlas significa o perder un día para
    siempre o volver a pedirlo en todos los arranques hasta el fin de los
    tiempos;
  - que un día que no se pudo ni preguntar NO deje fila, porque una fila vacía
    afirma que ese día no hubo nada y además se marca como no pendiente;
  - que un corte por límite pare y deje escrito lo anterior, porque son
    setecientas peticiones contra un servicio que corta por IP;
  - que lo recuperado quede marcado como recuperado.

La segunda mitad del archivo prueba el relleno de SALIDAS, que hasta ahora no
existía y por eso `activities` estuvo seis meses a cero mientras el bienestar se
rellenaba entero. Sus reglas son otras y están explicadas donde empieza.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.backfill import (
    METRICAS,
    PETICIONES_POR_DIA,
    SinCacheDeSalidas,
    dias_pendientes,
    rellenar,
    rellenar_salidas,
    reparsear_actividades,
    recuperar_al_arrancar,
    ventana_de_recuperacion,
)
from app.engine.signals import DayMetrics
from app.integrations.activity_cache import RUTA_CACHE_SALIDAS
from app.integrations.garmin import GarminRateLimited
from app.models import Activity, Base, DailyMetrics
from app.repository import upsert_daily_metrics
from tests.dobles import doble_de
from app.config_loader import Config
from app.integrations.garmin import GarminClient

HOY = date(2026, 9, 11)


@pytest.fixture
def db():
    """Base de datos en memoria, nueva para cada test."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def fila(session, dia: date, estado: str = "ok", **campos) -> None:
    """Una fila de wellness ya escrita, como la que dejaría un día normal."""
    session.add(DailyMetrics(date=dia, fetch_status=estado, **campos))
    session.commit()


def leer(session, dia: date) -> DailyMetrics | None:
    """La fila de ese día. La clave primaria es `id`, no la fecha."""
    return session.scalars(
        select(DailyMetrics).where(DailyMetrics.date == dia)
    ).one_or_none()


def metrics(dia: date, **campos) -> DayMetrics:
    """Un día de Garmin completo salvo lo que se pise por nombre."""
    base = dict(hrv=58.0, rhr=47.0, sleep_min=430, sleep_score=76, body_battery=72)
    base.update(campos)
    return DayMetrics(date=dia, **base)


@doble_de(GarminClient)
class ClienteFalso:
    """Un Garmin de mentira que contesta lo que se le diga, día a día.

    `respuestas` mapea día -> lo que hace: un `DayMetrics`, una excepción que
    lanzar, o una lista de textos que se apuntan en `fetch_errors` antes de
    devolver un día con huecos. Lo que no esté en el mapa sale completo.
    """

    def __init__(self, respuestas: dict | None = None) -> None:
        self.respuestas = respuestas or {}
        self.fetch_errors: list[str] = []
        self.pedidos: list[date] = []

    def day_metrics(self, day: date) -> DayMetrics:
        dia = day
        self.pedidos.append(dia)
        r = self.respuestas.get(dia)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            fallos, m = r
            self.fetch_errors.extend(fallos)
            return m
        return r if isinstance(r, DayMetrics) else metrics(dia)


def sin_dormir(_s: float) -> None:
    """El freno de verdad son 2 s por día. Aquí sobra y se cronometraría solo."""


# ---------------------------------------------------------------------------
# Qué cuenta como pendiente
# ---------------------------------------------------------------------------


def test_un_dia_sin_fila_esta_pendiente(db):
    assert dias_pendientes(db, HOY - timedelta(days=2), HOY) == [
        HOY - timedelta(days=2), HOY - timedelta(days=1), HOY,
    ]


def test_un_dia_con_fila_buena_no_se_vuelve_a_pedir(db):
    fila(db, HOY, "ok")
    assert dias_pendientes(db, HOY, HOY) == []


def test_una_fila_partial_NO_se_reintenta(db):
    """`partial` es "se preguntó, Garmin contestó, y de eso no había dato".

    Reintentarlo es gastar cuatro peticiones para recibir exactamente la misma
    respuesta, todos los arranques, para siempre. Contra un servicio que corta
    por IP eso no es ineficiencia: es la forma de que el corte sea permanente.

    Y no es una hipótesis: el body battery deja de servirse hacia atrás a los
    cuatro meses, así que en un backfill de 180 días hay un tramo entero de días
    que siempre van a volver incompletos.
    """
    fila(db, HOY, "partial")
    assert dias_pendientes(db, HOY, HOY) == []


def test_una_fila_error_SI_se_reintenta(db):
    """`error` es "no se pudo leer": un 500, un timeout, un 429.

    Sin separarlo de `partial`, un corte de red quedaba archivado para siempre
    como "esa noche no dormí con el reloj", y el análisis no tendría forma de
    distinguir un fallo de infraestructura de una noche sin reloj.
    """
    fila(db, HOY, "error")
    assert dias_pendientes(db, HOY, HOY) == [HOY]


def test_se_piden_solo_los_huecos_y_en_orden(db):
    d = [HOY - timedelta(days=i) for i in range(5, 0, -1)]
    fila(db, d[0], "ok")
    fila(db, d[2], "partial")
    fila(db, d[3], "error")
    assert dias_pendientes(db, d[0], d[-1]) == [d[1], d[3], d[4]]


def test_un_rango_del_reves_no_pide_nada(db):
    """Defensa contra el config raro, no contra el llamante distraído: con
    `recovery_days` a 1 la ventana acaba antes de empezar, y eso tiene que ser
    cero días y no un rango negativo interpretado al revés."""
    assert dias_pendientes(db, HOY, HOY - timedelta(days=3)) == []


# ---------------------------------------------------------------------------
# El relleno
# ---------------------------------------------------------------------------


def test_lo_recuperado_queda_marcado_como_recuperado(db):
    """`recovered_at` no es contabilidad: es lo que permite leer los huecos.

    Una fila rellenada meses después puede no tener body battery porque Garmin
    ya no lo sirve, no porque esa noche no hubiera reloj. Sin la marca, las dos
    ausencias son la misma celda vacía y el análisis no puede distinguirlas.
    """
    dia = HOY - timedelta(days=40)
    res = rellenar(db, ClienteFalso(), [dia], pausa=0, dormir=sin_dormir)

    assert res.escritos == [dia]
    f = leer(db, dia)
    assert f is not None and f.hrv == 58.0
    assert isinstance(f.recovered_at, datetime), (
        "sin la marca no se puede saber si un hueco es de la noche o de la fecha"
    )


def test_una_fila_escrita_en_su_dia_no_se_marca_al_repasarla(db):
    """El repaso del arranque no puede reescribir la historia de lo que ya había.

    Si `recovered_at` se estampara en cada upsert, la ventana diaria de
    `run_daily` -que relee siete días- acabaría marcando como recuperado todo lo
    capturado en el día, y la marca dejaría de significar nada.
    """
    dia = HOY - timedelta(days=3)
    upsert_daily_metrics(db, [metrics(dia)])
    db.commit()
    upsert_daily_metrics(db, [metrics(dia, hrv=61.0)], recuperado=True)
    db.commit()

    f = leer(db, dia)
    assert f.hrv == 61.0, "el dato nuevo sí entra"
    assert f.recovered_at is None, "pero la fila sigue siendo de las del día"


def test_lo_que_garmin_ya_no_sirve_queda_como_hueco_y_no_como_cero(db):
    """Un body battery ausente a -150 días es None, nunca 0.

    Un 0 sería un dato, y del peor tipo: "ese día amaneció sin batería" es una
    afirmación clínica, y entraría en las correlaciones de la vista 1 tirando de
    ellas hacia abajo sin que nada avisara.
    """
    dia = HOY - timedelta(days=150)
    c = ClienteFalso({dia: metrics(dia, body_battery=None)})
    res = rellenar(db, c, [dia], pausa=0, dormir=sin_dormir)

    f = leer(db, dia)
    assert f.body_battery is None
    assert f.hrv == 58.0, "lo que sí vino se guarda igual"
    assert f.fetch_status == "partial", (
        "se preguntó y Garmin contestó que no había: eso no se reintenta"
    )
    assert "body battery" in (f.fetch_error or "") or "body_battery" in (f.fetch_error or "")
    assert res.huecos["body_battery"] == [dia]
    assert res.escritos == [dia], "un hueco conocido no es un fallo"


def test_las_metricas_que_se_piden_son_las_cinco_que_hay(db):
    """Un día completo sale `ok`, y completo son CINCO campos, no seis.

    Aquí hubo un test distinto, `test_lo_que_no_se_pidio_no_cuenta_como_hueco`,
    y merece la pena contar por qué ya no hace falta. Existía una sexta métrica,
    training readiness, que se pedía y volvía siempre vacía porque este reloj no
    la calcula. Para que su ausencia no marcara `partial` todas las filas se
    inventó `not_requested`: una lista de campos que no cuentan como hueco.

    Esa lista era un parche sosteniendo a una columna que nunca tuvo un dato. Al
    quitar la columna se fue el parche, y el invariante que queda es más simple y
    más fuerte: las cinco métricas se piden las cinco, y si vuelven las cinco la
    fila es `ok` sin excepciones que negociar.

    `METRICAS` se comprueba aquí porque es la lista contra la que
    `rellenar` cuenta huecos. Si alguien añade una sexta sin que haya dato
    detrás, vuelve el problema entero: filas `partial` permanentes que, al no
    reintentarse, se quedan estables y calladas para siempre.
    """
    assert METRICAS == ("hrv", "rhr", "sleep_min", "sleep_score", "body_battery")

    dia = HOY - timedelta(days=10)
    rellenar(db, ClienteFalso(), [dia], pausa=0, dormir=sin_dormir)

    f = leer(db, dia)
    assert f.fetch_status == "ok", f"un día completo no puede ser {f.fetch_error}"
    assert f.fetch_error is None


def test_un_dia_que_no_se_pudo_preguntar_NO_deja_fila(db):
    """Escribir una fila vacía sería mentir dos veces.

    Primero porque afirma "ese día no hubo nada" cuando lo que pasó es que no se
    pudo preguntar. Y segundo porque una fila sin `error` ya no está pendiente:
    el día quedaría perdido para siempre, y perdido en silencio.
    """
    dia = HOY - timedelta(days=20)
    c = ClienteFalso({dia: RuntimeError("500 del servidor")})
    res = rellenar(db, c, [dia], pausa=0, dormir=sin_dormir)

    assert leer(db, dia) is None
    assert res.fallidos == [dia] and res.escritos == []
    assert "500 del servidor" in res.errores[0]
    assert dias_pendientes(db, dia, dia) == [dia], "y se reintenta solo"


def test_un_fallo_de_una_sola_metrica_deja_la_fila_en_error(db):
    """Distinto del caso anterior: la petición del día sí se hizo.

    Vinieron cuatro respuestas y una reventó. Lo que llegó se guarda -tirarlo
    sería perder tres métricas buenas- pero el día queda marcado `error` para
    que el siguiente arranque lo vuelva a intentar entero.
    """
    dia = HOY - timedelta(days=8)
    c = ClienteFalso({dia: (["2026-09-03: HRV ilegible"], metrics(dia, hrv=None))})
    res = rellenar(db, c, [dia], pausa=0, dormir=sin_dormir)

    f = leer(db, dia)
    assert f is not None and f.rhr == 47.0, "lo que llegó no se tira"
    assert f.fetch_status == "error"
    assert "HRV ilegible" in f.fetch_error
    assert res.fallidos == [dia] and res.escritos == []
    assert dias_pendientes(db, dia, dia) == [dia]


def test_los_apuntes_de_un_dia_no_se_atribuyen_al_siguiente(db):
    """`fetch_errors` se acumula en el cliente entre llamadas.

    Si se leyera la lista entera en vez del trozo nuevo, el primer fallo
    marcaría `error` en todos los días posteriores y el backfill volvería a
    pedirlos enteros en cada arranque.
    """
    d1, d2 = HOY - timedelta(days=6), HOY - timedelta(days=5)
    c = ClienteFalso({d1: (["d1: HRV ilegible"], metrics(d1, hrv=None))})
    res = rellenar(db, c, [d1, d2], pausa=0, dormir=sin_dormir)

    assert leer(db, d1).fetch_status == "error"
    assert leer(db, d2).fetch_status == "ok", (
        "el apunte del día anterior seguía en la lista del cliente"
    )
    assert res.escritos == [d2] and res.fallidos == [d1]
    assert res.errores == ["d1: HRV ilegible"]


# ---------------------------------------------------------------------------
# Que termine, y que se pueda reanudar
# ---------------------------------------------------------------------------


def test_un_corte_por_limite_para_y_deja_escrito_lo_anterior(db):
    """Son cuatro peticiones por día y Garmin corta por IP durante minutos.

    Seguir pidiendo tras un 429 no arregla nada -el corte es de la IP, no del
    día- y alarga el castigo. Y con una sola transacción al final, el corte del
    día 150 tiraría los 149 anteriores: la siguiente ejecución los volvería a
    pedir, que es la forma más rápida de que el corte sea permanente.
    """
    dias = [HOY - timedelta(days=i) for i in range(5, 0, -1)]
    c = ClienteFalso({dias[2]: GarminRateLimited("429")})
    res = rellenar(db, c, dias, pausa=0, dormir=sin_dormir)

    assert res.escritos == dias[:2], "lo anterior al corte queda guardado"
    assert c.pedidos == dias[:3], "y no se sigue insistiendo"
    assert res.interrumpido and "límite" in res.interrumpido

    # Y lo escrito está en disco de verdad, no pendiente de un commit que ya no
    # va a llegar: se comprueba desde una sesión nueva sobre el mismo motor.
    with Session(db.get_bind()) as otra:
        assert dias_pendientes(otra, dias[0], dias[-1]) == dias[2:]


def test_relanzarlo_sigue_por_donde_se_quedo(db):
    """La reanudación no lleva contabilidad en ninguna parte: la deduce."""
    dias = [HOY - timedelta(days=i) for i in range(5, 0, -1)]
    rellenar(
        db, ClienteFalso({dias[2]: GarminRateLimited("429")}), dias,
        pausa=0, dormir=sin_dormir,
    )

    pendientes = dias_pendientes(db, dias[0], dias[-1])
    c2 = ClienteFalso()
    res = rellenar(db, c2, pendientes, pausa=0, dormir=sin_dormir)

    assert c2.pedidos == dias[2:], "no se vuelve a pedir lo que ya está"
    assert res.escritos == dias[2:]
    assert dias_pendientes(db, dias[0], dias[-1]) == []


def test_un_fallo_normal_no_para_el_trabajo(db):
    """Un 500 suelto es del día, no de la IP: se apunta y se sigue."""
    dias = [HOY - timedelta(days=i) for i in range(4, 0, -1)]
    c = ClienteFalso({dias[1]: RuntimeError("500")})
    res = rellenar(db, c, dias, pausa=0, dormir=sin_dormir)

    assert c.pedidos == dias
    assert res.interrumpido is None
    assert res.escritos == [dias[0], dias[2], dias[3]]
    assert res.fallidos == [dias[1]]


def test_la_pausa_va_entre_dias_y_no_despues_del_ultimo(db):
    """Veinticinco minutos de trabajo no pueden llevar una pausa de regalo."""
    dormidas: list[float] = []
    dias = [HOY - timedelta(days=i) for i in range(3, 0, -1)]
    rellenar(db, ClienteFalso(), dias, pausa=2.0, dormir=dormidas.append)
    assert dormidas == [2.0, 2.0]


def test_sin_dias_no_se_toca_a_garmin(db):
    c = ClienteFalso()
    res = rellenar(db, c, [], pausa=0, dormir=sin_dormir)
    assert c.pedidos == [] and res.peticiones == 0


def test_el_coste_se_puede_decir_de_antemano(db):
    dias = [HOY - timedelta(days=i) for i in range(3, 0, -1)]
    res = rellenar(db, ClienteFalso(), dias, pausa=0, dormir=sin_dormir)
    assert res.peticiones == 3 * PETICIONES_POR_DIA


def test_el_resumen_dice_lo_que_paso(db):
    dias = [HOY - timedelta(days=i) for i in range(3, 0, -1)]
    c = ClienteFalso({dias[1]: RuntimeError("500")})
    texto = rellenar(db, c, dias, pausa=0, dormir=sin_dormir).resumen()
    assert "2 día(s) escritos de 3 pedidos" in texto
    assert "1 fallaron" in texto


# ---------------------------------------------------------------------------
# El repaso del arranque
# ---------------------------------------------------------------------------


def cfg_backfill(recovery_days=45, pause_seconds=0.0):
    @doble_de(Config)
    class Cfg:
        def __init__(self):
            self.raw = {"wellness": {"backfill": {
                "recovery_days": recovery_days, "pause_seconds": pause_seconds,
            }}}

    return Cfg()


def test_la_ventana_acaba_anteayer(db):
    """Ni hoy ni ayer, y no es prudencia de más.

    Los datos de anoche llegan al servidor de Garmin cuando el reloj sincroniza,
    que puede ser a media mañana. Pedirlos al arrancar devolvería medias tintas,
    y como una fila `partial` NO se reintenta, esas medias tintas se quedarían
    archivadas como definitivas. De los dos últimos días se encarga la ventana
    diaria de `run_daily`, que relee siete y sí puede corregirse a sí misma.
    """
    desde, hasta = ventana_de_recuperacion(cfg_backfill(10), HOY)
    assert hasta == HOY - timedelta(days=2)
    assert desde == HOY - timedelta(days=10)


def test_con_recovery_days_a_cero_el_repaso_esta_apagado(db):
    assert ventana_de_recuperacion(cfg_backfill(0), HOY) is None
    c = ClienteFalso()
    res = recuperar_al_arrancar(db, c, cfg_backfill(0), hoy=HOY, dormir=sin_dormir)
    assert c.pedidos == [] and res.pedidos == []


def test_el_repaso_del_arranque_pide_lo_que_falta_y_solo_eso(db):
    ayer = HOY - timedelta(days=1)
    anteayer = HOY - timedelta(days=2)
    fila(db, HOY - timedelta(days=3), "ok")

    c = ClienteFalso()
    res = recuperar_al_arrancar(db, c, cfg_backfill(5), hoy=HOY, dormir=sin_dormir)

    esperados = [HOY - timedelta(days=i) for i in (5, 4, 2)]
    assert c.pedidos == esperados, f"pidió {c.pedidos}"
    assert HOY not in c.pedidos and ayer not in c.pedidos
    assert res.escritos == esperados
    assert all(
        leer(db, d).recovered_at is not None for d in esperados
    )
    assert anteayer in esperados


def test_si_no_falta_nada_el_repaso_no_pide_nada(db):
    for i in range(2, 6):
        fila(db, HOY - timedelta(days=i), "ok")
    c = ClienteFalso()
    res = recuperar_al_arrancar(db, c, cfg_backfill(5), hoy=HOY, dormir=sin_dormir)
    assert c.pedidos == [] and res.pedidos == []


def test_el_repaso_usa_la_pausa_del_config(db):
    dormidas: list[float] = []
    recuperar_al_arrancar(
        db, ClienteFalso(), cfg_backfill(5, pause_seconds=1.5),
        hoy=HOY, dormir=dormidas.append,
    )
    assert dormidas == [1.5, 1.5, 1.5], f"cuatro días pedidos, tres pausas: {dormidas}"


# ---------------------------------------------------------------------------
# La otra mitad: las salidas que llevaban meses en disco sin llegar a la tabla
# ---------------------------------------------------------------------------
#
# Esto no se probó nunca porque no existía, y no existía por un olvido que no
# tenía forma de verse: `upsert_activities` tenía un solo llamador -el trabajo
# diario, que archiva las salidas del día que está decidiendo-, así que el
# backfill de seis meses cubrió el bienestar y dejó `activities` a cero. Por
# fuera las dos cosas se llamaban lo mismo.
#
# Lo que se prueba aquí es lo que decide si el relleno sirve o hace daño:
#
#   - que NO reescriba la clasificación de una salida ya archivada, porque
#     `intensa` significa "intensa según los umbrales del día en que se hizo" y
#     re-estampar el histórico con el config de hoy cambiaría de significado lo
#     que ya está medido, sin un error y sin manera de volver atrás;
#   - que una caché que falta sea un error y no un resultado de cero, que es
#     indistinguible de "ya estaba todo archivado";
#   - que una salida sin `activity_id` se cuente en vez de desaparecer;
#   - que `--simular` cuente exactamente lo que luego se escribe.


CICLISMO = {"activityType": {"typeKey": "cycling"}}

CFG_BICI = {
    "cycling": {
        "activity_types": ["cycling"],
        "classification": [
            {"level": "intensa", "zones": [4, 5], "min_time_pct": 30},
            {"level": "media", "zones": [3], "min_time_pct": 30},
            {"level": "suave", "always": True},
        ],
        "classification_fallback": {
            "use": "anaerobic_training_effect",
            "intensa_if_gte": 2.0,
            "media_if_gte": 1.0,
            "on_no_data": "desconocida",
        },
        "load": {"fallback_estimate": {
            "enabled": True,
            "load_per_hour": {"suave": 40, "media": 90, "intensa": 160},
        }},
    }
}


def actividad(dia: date, aid: int | None = 1, *, z4z5: float = 0.0, **extra):
    """Una actividad cruda de Garmin, del tamaño mínimo que el parser acepta.

    El reparto por zonas se da con un solo mando -`z4z5`, el porcentaje de
    tiempo duro- porque es lo único que decide la etiqueta, y escribir las cinco
    zonas a mano en cada test escondería cuál de los cinco números importa.
    """
    act = {
        **CICLISMO,
        "startTimeLocal": f"{dia.isoformat()} 09:00:00",
        "duration": 3600.0,
        "distance": 30000.0,
        "activityTrainingLoad": 120.0,
        "movingDuration": 3400.0,
        "elevationGain": 400.0,
        "averageHR": 140.0,
        "hrTimeInZone_1": 0.0,
        "hrTimeInZone_2": 3600.0 * (1 - z4z5 / 100.0),
        "hrTimeInZone_3": 0.0,
        "hrTimeInZone_4": 3600.0 * (z4z5 / 100.0),
        "hrTimeInZone_5": 0.0,
    }
    if aid is not None:
        act["activityId"] = aid
    act.update(extra)
    return act


def cache_con(tmp_path, actividades) -> Path:
    ruta = tmp_path / "activities.json"
    ruta.write_text(json.dumps(actividades), encoding="utf-8")
    return ruta


def test_el_relleno_archiva_las_salidas_que_no_tenian_fila(db, tmp_path):
    ruta = cache_con(tmp_path, [
        actividad(date(2026, 3, 7), 11, z4z5=50),
        actividad(date(2026, 5, 2), 22, z4z5=0),
        actividad(date(2026, 9, 6), 33, z4z5=40),
    ])

    res = rellenar_salidas(db, CFG_BICI, ruta)

    assert res.escritas == 3
    assert res.ya_estaban == 0
    assert (res.primera, res.ultima) == (date(2026, 3, 7), date(2026, 9, 6))
    assert res.niveles == {"intensa": 2, "suave": 1}

    filas = db.scalars(select(Activity).order_by(Activity.date)).all()
    assert [f.garmin_activity_id for f in filas] == [11, 22, 33]
    assert [f.intensity_level for f in filas] == ["intensa", "suave", "intensa"]


def test_el_relleno_guarda_las_columnas_que_solo_usa_el_analisis(db, tmp_path):
    """Desnivel, tiempo en movimiento y FC media, que no las usa el motor.

    Van aparte porque son justo las que se quedaron a NULL durante meses: el
    motor no las lee, así que ninguna decisión salía mal y nadie las echaba de
    menos. Las lee la vista 5, que escribe frases con ellas.
    """
    ruta = cache_con(tmp_path, [actividad(date(2026, 4, 1), 7)])
    rellenar_salidas(db, CFG_BICI, ruta)

    f = db.scalars(select(Activity)).one()
    assert f.elevation_gain_m == 400.0
    assert f.moving_duration_s == 3400.0
    assert f.avg_hr == 140.0
    assert f.training_load == 120.0
    assert f.training_load_estimated is False


def test_el_relleno_no_reescribe_la_clasificacion_de_lo_ya_archivado(db, tmp_path):
    """La etiqueta de abril es la de los umbrales de abril, y así se queda.

    En `activities` se guarda la CLASIFICACIÓN, no solo el crudo, porque depende
    del `config.yaml` del día en que se hizo. Si el relleno reclasificara, cada
    ejecución re-estamparía el histórico entero con los umbrales de hoy y "el
    lumbar sube después de una salida intensa" pasaría a medirse contra unas
    intensas que en su momento no lo fueron. Sin error y sin vuelta atrás.
    """
    db.add(Activity(
        garmin_activity_id=99, date=date(2026, 4, 1),
        intensity_level="intensa", classification_source="zones",
    ))
    db.commit()

    # La misma salida, pero con un reparto por zonas que hoy daría `suave`.
    ruta = cache_con(tmp_path, [actividad(date(2026, 4, 1), 99, z4z5=0)])
    res = rellenar_salidas(db, CFG_BICI, ruta)

    assert res.escritas == 0
    assert res.ya_estaban == 1
    assert db.scalars(select(Activity)).one().intensity_level == "intensa"


def test_el_relleno_archiva_lo_nuevo_sin_tocar_lo_viejo(db, tmp_path):
    db.add(Activity(
        garmin_activity_id=99, date=date(2026, 4, 1), intensity_level="intensa",
    ))
    db.commit()

    ruta = cache_con(tmp_path, [
        actividad(date(2026, 4, 1), 99, z4z5=0),
        actividad(date(2026, 4, 8), 100, z4z5=0),
    ])
    res = rellenar_salidas(db, CFG_BICI, ruta)

    assert (res.escritas, res.ya_estaban) == (1, 1)
    filas = {
        f.garmin_activity_id: f.intensity_level for f in db.scalars(select(Activity))
    }
    assert filas == {99: "intensa", 100: "suave"}


def test_repetir_el_relleno_no_escribe_nada_la_segunda_vez(db, tmp_path):
    ruta = cache_con(tmp_path, [actividad(date(2026, 4, 1), 7)])
    assert rellenar_salidas(db, CFG_BICI, ruta).escritas == 1
    segunda = rellenar_salidas(db, CFG_BICI, ruta)
    assert (segunda.escritas, segunda.ya_estaban) == (0, 1)
    assert len(db.scalars(select(Activity)).all()) == 1


def test_sin_cache_el_relleno_es_un_error_y_no_un_resultado_de_cero(db, tmp_path):
    """Cero salidas archivadas y "no hay fichero" no son lo mismo.

    Para el MOTOR una caché que falta es un histórico más corto y la mañana
    sigue; por eso `load_cached_rides` no lanza. Para el RELLENO no hay nada a
    lo que degradar: se ha invocado para archivar un histórico y no ha hecho
    nada. Devolver un resultado vacío sería indistinguible de "ya estaba todo",
    que es exactamente la confusión que dejó la tabla a cero durante seis meses.
    """
    with pytest.raises(SinCacheDeSalidas):
        rellenar_salidas(db, CFG_BICI, tmp_path / "no_esta.json")


def test_una_cache_sin_ninguna_salida_en_bici_tambien_es_un_error(db, tmp_path):
    ruta = cache_con(tmp_path, [
        {"activityType": {"typeKey": "running"}, "activityId": 1,
         "startTimeLocal": "2026-04-01 09:00:00"},
    ])
    with pytest.raises(SinCacheDeSalidas) as exc:
        rellenar_salidas(db, CFG_BICI, ruta)
    assert "1 actividad" in str(exc.value)


def test_una_salida_sin_activity_id_se_cuenta_en_vez_de_desaparecer(db, tmp_path):
    """Sin id no se puede guardar, pero tiene que constar que existía.

    `upsert_activities` se las salta a propósito: dos salidas del mismo día sin
    id se fundirían en una y la carga del día bajaría. Lo que no puede pasar es
    que el informe diga "una de una archivada" cuando eran dos.
    """
    ruta = cache_con(tmp_path, [
        actividad(date(2026, 4, 1), 7),
        actividad(date(2026, 4, 2), None),
    ])
    res = rellenar_salidas(db, CFG_BICI, ruta)

    assert res.salidas == 2
    assert res.escritas == 1
    assert res.sin_id == 1
    assert "sin activity_id" in res.resumen()


def test_un_activity_id_repetido_en_el_fichero_se_cuenta(db, tmp_path):
    """No debería pasar -la caché fusiona por id al escribir-, y por eso se dice.

    Si un día pasa, colapsar en silencio haría que el informe prometiera dos
    salidas archivadas cuando hay una sola fila.
    """
    ruta = cache_con(tmp_path, [
        actividad(date(2026, 4, 1), 7),
        actividad(date(2026, 4, 1), 7, z4z5=90),
    ])
    res = rellenar_salidas(db, CFG_BICI, ruta)

    assert res.repetidas == 1
    assert res.escritas == 1
    assert len(db.scalars(select(Activity)).all()) == 1
    assert "repetido" in res.resumen()


def test_simular_cuenta_lo_mismo_que_luego_se_escribe_y_no_escribe(db, tmp_path):
    """El censo tiene que salir de un solo sitio.

    Si la simulación contara por su cuenta, el día que los dos caminos
    discreparan la previsualización prometería una cosa y la ejecución haría
    otra, que es la única forma de que un `--simular` haga daño.
    """
    ruta = cache_con(tmp_path, [
        actividad(date(2026, 3, 7), 11, z4z5=50),
        actividad(date(2026, 5, 2), 22, z4z5=0),
    ])

    seco = rellenar_salidas(db, CFG_BICI, ruta, simular=True)
    assert seco.simulado is True
    assert db.scalars(select(Activity)).all() == []
    assert "se archivarían" in seco.resumen()

    mojado = rellenar_salidas(db, CFG_BICI, ruta)
    assert mojado.simulado is False
    assert (seco.escritas, seco.niveles) == (mojado.escritas, mojado.niveles)
    assert (seco.primera, seco.ultima) == (mojado.primera, mojado.ultima)
    assert len(db.scalars(select(Activity)).all()) == 2


def test_la_carga_estimada_se_archiva_marcada(db, tmp_path):
    """Un número estimado y uno medido no se pueden promediar como si fueran igual.

    La marca tiene que viajar hasta la tabla o el análisis no tiene forma de
    separarlos.
    """
    ruta = cache_con(tmp_path, [
        actividad(date(2026, 4, 1), 7, activityTrainingLoad=None),
    ])
    rellenar_salidas(db, CFG_BICI, ruta)

    f = db.scalars(select(Activity)).one()
    assert f.training_load_estimated is True
    assert f.training_load == 40.0  # una hora suave, a 40 de carga por hora


def test_la_ruta_de_la_cache_de_salidas_es_una_sola_y_esta_declarada():
    """Estaba escrita a mano en tres sitios, y ahora la lee también el relleno.

    Cuatro copias de una ruta son tres que se quedan atrás el día que se mueva,
    y el síntoma no sería un error: `load_cached_rides` devuelve "no existe" sin
    lanzar, así que el sitio no actualizado se quedaría con una caché vacía y
    seguiría funcionando, peor.
    """
    assert RUTA_CACHE_SALIDAS.name == "activities.json"
    assert RUTA_CACHE_SALIDAS.parent.name == "cache"
    raiz = Path(__file__).resolve().parents[1]
    for modulo in ("app/cli.py", "app/scheduler.py", "app/backfill.py"):
        texto = (raiz / modulo).read_text(encoding="utf-8")
        assert '"activities.json"' not in texto, (
            f"{modulo} vuelve a construir la ruta de la caché a mano"
        )


# ---------------------------------------------------------------------------
# Reparseo: llenar columnas que no existían cuando se archivó la fila
# ---------------------------------------------------------------------------
#
# Una columna nueva nace vacía para todo el histórico. El dato no se ha perdido
# -`data/cache/activities.json` guarda el resumen entero de cada actividad y no
# se poda nunca-, y esa decisión es justo la que hace que añadir una columna
# cueste un reparseo local en vez de volver a bajar seis meses contra un
# servidor que corta por 429.
#
# Las reglas son otras que las del relleno, y por eso están aparte: el relleno
# CREA filas y esto solo completa las que ya hay.


def test_el_reparseo_llena_las_columnas_que_estaban_a_nulo(db, tmp_path):
    """El caso para el que existe: diez columnas nuevas sobre filas ya archivadas."""
    db.add(Activity(
        garmin_activity_id=7, date=date(2026, 4, 1),
        intensity_level="suave", classification_source="zones",
    ))
    db.commit()

    ruta = cache_con(tmp_path, [actividad(
        date(2026, 4, 1), 7, maxHR=171.0, maxTemperature=34.0, calories=742.0,
    )])
    res = reparsear_actividades(db, ruta)

    assert res.emparejadas == 1
    f = db.scalars(select(Activity)).one()
    assert f.max_hr == 171.0
    assert f.max_temp_c == 34.0
    assert f.calories == 742.0
    assert f.elevation_gain_m == 400.0, "también las que ya sabía leer"


def test_el_reparseo_no_pisa_una_celda_que_ya_tiene_valor(db, tmp_path):
    """Solo escribe donde hay un NULL, y esto es lo que lo hace repetible.

    Si pisara, cada ejecución re-estamparía el histórico con lo que diga la
    caché de hoy. Y la caché se fusiona en cada refresco: un campo que Garmin
    recalcule más tarde reescribiría hacia atrás una fila que ya se usó para
    decidir, sin dejar rastro de que el número ha cambiado.
    """
    db.add(Activity(
        garmin_activity_id=7, date=date(2026, 4, 1), avg_hr=138.0,
    ))
    db.commit()

    ruta = cache_con(tmp_path, [actividad(date(2026, 4, 1), 7, averageHR=999.0)])
    res = reparsear_actividades(db, ruta)

    assert db.scalars(select(Activity)).one().avg_hr == 138.0
    assert res.ya_estaban["avg_hr"] == 1
    assert "avg_hr" not in res.rellenadas


def test_el_reparseo_no_toca_la_clasificacion_ni_la_carga(db, tmp_path):
    """La etiqueta de abril es la de los umbrales de abril. Igual que el relleno.

    `training_load` se queda fuera aunque VENGA en el crudo, que es el detalle
    que hace falta escribir: su columna la escribe la clasificación -con el
    `config.yaml` del día- y no el parseo. Si entrara en la lista de campos, un
    reparseo rellenaría con carga medida las filas que en su momento se
    archivaron con carga estimada, y la marca `training_load_estimated` seguiría
    diciendo que era estimada.
    """
    db.add(Activity(
        garmin_activity_id=7, date=date(2026, 4, 1),
        intensity_level="intensa", classification_source="zones",
        training_load=None, training_load_estimated=True,
    ))
    db.commit()

    ruta = cache_con(tmp_path, [actividad(date(2026, 4, 1), 7, z4z5=0)])
    res = reparsear_actividades(db, ruta)

    f = db.scalars(select(Activity)).one()
    assert f.intensity_level == "intensa", "la salida de abril sigue siendo la de abril"
    assert f.training_load is None, "la carga la escribe la clasificación, no esto"
    assert "training_load" not in res.rellenadas
    assert "training_load" not in res.sin_dato


def test_el_reparseo_no_archiva_las_salidas_sin_fila(db, tmp_path):
    """Crear historia es `rellenar_salidas`. Son dos operaciones distintas.

    Y la diferencia se cuenta en vez de callarse: `sin_fila` es lo que separa
    "no había columna" de "no había salida", que llevan a dos arreglos
    distintos.
    """
    ruta = cache_con(tmp_path, [
        actividad(date(2026, 4, 1), 7),
        actividad(date(2026, 4, 8), 8),
    ])

    res = reparsear_actividades(db, ruta)

    assert res.sin_fila == 2
    assert res.emparejadas == 0
    assert db.scalars(select(Activity)).all() == []


def test_el_reparseo_distingue_no_se_guardo_de_garmin_no_lo_mando(db, tmp_path):
    """Un rodillo de interior no tiene temperatura, y eso no es un fallo.

    Sin esta separación las dos se leerían igual -celda vacía después de
    reparsear- y no habría forma de saber si hay que arreglar algo o si la
    salida simplemente no llevaba ese dato.
    """
    db.add(Activity(garmin_activity_id=7, date=date(2026, 4, 1)))
    db.commit()

    # Sin ninguna de las dos temperaturas, que es como viene un rodillo.
    ruta = cache_con(tmp_path, [actividad(date(2026, 4, 1), 7, maxHR=171.0)])
    res = reparsear_actividades(db, ruta)

    assert res.rellenadas["max_hr"] == 1
    assert res.sin_dato["max_temp_c"] == 1
    assert "max_temp_c" not in res.rellenadas


def test_el_reparseo_simulado_promete_exactamente_lo_que_hara(db, tmp_path):
    """Mismo criterio que el del relleno: el censo sale de un solo sitio."""
    db.add(Activity(garmin_activity_id=7, date=date(2026, 4, 1)))
    db.commit()

    ruta = cache_con(tmp_path, [actividad(date(2026, 4, 1), 7, maxHR=171.0)])

    seco = reparsear_actividades(db, ruta, simular=True)
    assert seco.simulado is True
    assert "se rellenarían" in seco.resumen()
    assert db.scalars(select(Activity)).one().max_hr is None, "ha escrito simulando"

    mojado = reparsear_actividades(db, ruta)
    assert seco.rellenadas == mojado.rellenadas
    assert db.scalars(select(Activity)).one().max_hr == 171.0


def test_repetir_el_reparseo_no_rellena_nada_la_segunda_vez(db, tmp_path):
    db.add(Activity(garmin_activity_id=7, date=date(2026, 4, 1)))
    db.commit()
    ruta = cache_con(tmp_path, [actividad(date(2026, 4, 1), 7, maxHR=171.0)])

    assert reparsear_actividades(db, ruta).rellenadas["max_hr"] == 1
    segunda = reparsear_actividades(db, ruta)
    assert "max_hr" not in segunda.rellenadas
    assert segunda.ya_estaban["max_hr"] == 1


def test_el_reparseo_mira_los_campos_que_copia_el_upsert_y_no_una_lista_propia():
    """Dos listas son una lista desactualizada.

    Si el reparseo tuviera su propio inventario de campos, la columna número
    once entraría en `CAMPOS_ACTIVIDAD` -y se guardaría desde ese día- pero no
    aquí, así que el histórico se quedaría sin ella para siempre. Y el síntoma
    sería el mismo que motivó todo esto: una columna llena hacia delante y vacía
    hacia atrás, sin nada que lo diga.
    """
    from app.repository import CAMPOS_ACTIVIDAD

    raiz = Path(__file__).resolve().parents[1]
    texto = (raiz / "app" / "backfill.py").read_text(encoding="utf-8")
    assert "CAMPOS_ACTIVIDAD" in texto, "el reparseo se ha hecho su propia lista"
    assert "training_load" in CAMPOS_ACTIVIDAD, (
        "si la carga deja de copiarse aquí, el filtro del reparseo sobra"
    )


def test_sin_cache_el_reparseo_es_un_error_y_no_un_resultado_de_cero(db, tmp_path):
    """Un cero silencioso aquí se leería como "ya estaba todo relleno"."""
    with pytest.raises(SinCacheDeSalidas):
        reparsear_actividades(db, tmp_path / "no_esta.json")
