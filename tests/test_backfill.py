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
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.backfill import (
    PETICIONES_POR_DIA,
    dias_pendientes,
    rellenar,
    recuperar_al_arrancar,
    ventana_de_recuperacion,
)
from app.engine.signals import DayMetrics
from app.integrations.garmin import GarminRateLimited
from app.models import Base, DailyMetrics
from app.repository import upsert_daily_metrics

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
    return DayMetrics(date=dia, not_requested=("readiness",), **base)


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

    def day_metrics(self, dia: date) -> DayMetrics:
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


def test_lo_que_no_se_pidio_no_cuenta_como_hueco(db):
    """`readiness` está apagado a propósito: su ausencia no es una incidencia.

    Sin esta distinción TODAS las filas quedarían `partial` para siempre, y
    `partial` dejaría de servir para lo único que sirve -señalar las que de
    verdad les falta algo-. Encima, al no reintentarse las `partial`, el estado
    sería estable y silencioso: nadie se enteraría nunca.
    """
    dia = HOY - timedelta(days=10)
    rellenar(db, ClienteFalso(), [dia], pausa=0, dormir=sin_dormir)

    f = leer(db, dia)
    assert f.fetch_status == "ok", f"readiness no se pidió: {f.fetch_error}"
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
    class Cfg:
        raw = {"wellness": {"backfill": {
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
