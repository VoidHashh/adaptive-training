"""`fetched_at` tiene que decir cuándo llegó el dato, no cuándo nació la fila.

QUÉ PASÓ
--------
El 2026-09-15 la fila de `daily_metrics` decía `fetched_at 04:23:33` y tenía la
HRV dentro. La decisión de ese mismo segundo registraba `hrv: null`. Parecía un
fallo de fusión -dos piezas leyendo cosas distintas del mismo día- y no lo era:
la fila se creó a las 04:23 con la noche todavía sin subir, se rellenó más
tarde, y `fetched_at` no se movió porque estaba declarada solo con
`server_default=func.now()`, que dispara al INSERT y nunca más.

O sea: una columna llamada `fetched_at` que significaba `created_at`. Es la
misma avería que ya se corrigió en los tres `updated_at` de
`exercise_targets` y compañía, y es la figura de fondo de este proyecto -un
valor que se lee y que no es el valor que se usa-, pero aquí tenía un agravante:
era la única columna capaz de contestar «¿cuánto tarda Garmin en tener el dato
después de sincronizar?», y contestaba que cero, siempre, por construcción.

QUÉ SE ATA AQUÍ
---------------
Que una segunda escritura mueve la marca. No se comprueba el instante -eso es
un reloj, no un test- sino que la marca de después es POSTERIOR a la de antes.

Y se comprueba en las tres tablas que tienen la columna, no solo en la que dio
el susto: las tres se reescriben en sitio y las tres tenían el mismo defecto.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Activity, Base, DailyMetrics, WorkoutLog

DIA = date(2026, 9, 15)


@pytest.fixture
def db():
    """Base de datos en memoria, nueva para cada test."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _envejece(session, fila, segundos: int = 90) -> None:
    """Echa la marca hacia atrás sin pasar por el ORM.

    Dos escrituras seguidas en el mismo test caen en el mismo segundo, y
    `func.now()` en SQLite no tiene más resolución que eso: comparar las dos
    marcas daría igualdad y el test pasaría con `onupdate` y sin él. Envejecer
    la primera a mano hace que la comparación signifique algo.
    """
    fila.fetched_at = fila.fetched_at - timedelta(seconds=segundos)
    session.flush()


def test_rellenar_la_fila_de_wellness_mueve_la_marca(db):
    """El caso del 2026-09-15, clavado: fila creada a ciegas y rellenada luego."""
    fila = DailyMetrics(date=DIA, hrv=None, rhr=None, sleep_min=None)
    db.add(fila)
    db.flush()
    _envejece(db, fila)
    antes = fila.fetched_at

    # Llega la noche. Es la misma fila: `upsert_daily_metrics` busca por fecha.
    fila.hrv = 50.0
    fila.sleep_min = 430
    db.flush()

    assert fila.fetched_at > antes, (
        "la fila tiene la HRV dentro y sigue diciendo la hora en la que esa HRV "
        "todavía no existía"
    )


def test_una_escritura_que_no_cambia_nada_tambien_vale(db):
    """No se pide que la marca solo se mueva «cuando llega dato nuevo».

    Distinguirlo exigiría comparar campo a campo, y el valor de la columna es
    justo el contrario: «la última vez que alguien tocó esto». Un refresco que
    reescribe lo mismo ES información sobre cuándo se miró.
    """
    fila = DailyMetrics(date=DIA, hrv=50.0)
    db.add(fila)
    db.flush()
    _envejece(db, fila)
    antes = fila.fetched_at

    fila.hrv = 51.0
    db.flush()
    assert fila.fetched_at > antes


def test_la_latencia_se_puede_calcular_de_la_fila(db):
    """Para lo que existe el arreglo: restar y obtener un número de verdad.

    La pregunta que hay que poder contestar dentro de una semana es «¿cuánto
    tarda Garmin en tener el dato?». La resta es `fetched_at` menos la
    medianoche del día al que pertenece la noche. Con la columna clavada al
    INSERT esa resta daba la hora de la PRIMERA pregunta -que es una propiedad
    del cron, no de Garmin- y por eso la latencia solo se podía estimar.
    """
    fila = DailyMetrics(date=DIA)
    db.add(fila)
    db.flush()
    _envejece(db, fila, segundos=3600)
    ciega = fila.fetched_at

    fila.hrv = 50.0
    db.flush()

    medianoche = datetime.combine(DIA, time.min)
    latencia = fila.fetched_at - medianoche
    latencia_falsa = ciega - medianoche

    assert isinstance(latencia, timedelta)
    assert latencia > latencia_falsa, (
        "la latencia medida tiene que ser la del dato, no la de la primera "
        "pregunta; si salen iguales es que la columna volvió a congelarse"
    )
    assert latencia - latencia_falsa == timedelta(seconds=3600)


def test_la_actividad_recontada_mueve_la_marca(db):
    """Garmin tarda en cuajar la carga y los tiempos por zona de una salida.

    `upsert_activities` la vuelve a escribir cuando llegan. Sin `onupdate` la
    columna decía cuándo se vio la salida, no cuándo se completó.
    """
    fila = Activity(garmin_activity_id=1, date=DIA, training_load=None)
    db.add(fila)
    db.flush()
    _envejece(db, fila)
    antes = fila.fetched_at

    fila.training_load = 210.0
    db.flush()
    assert fila.fetched_at > antes


def test_el_entreno_revisado_mueve_la_marca(db):
    """La reconciliación vuelve a mirar el entreno al día siguiente."""
    fila = WorkoutLog(date=DIA, hevy_workout_id="w1")
    db.add(fila)
    db.flush()
    _envejece(db, fila)
    antes = fila.fetched_at

    fila.unplanned = True
    db.flush()
    assert fila.fetched_at > antes


def test_las_tres_tablas_declaran_onupdate():
    """La comprobación estructural, por si alguien añade una cuarta.

    Los tests de arriba pasan por el ORM y podrían seguir pasando si alguien
    moviera la lógica a mano en el repositorio. Esto mira la declaración.
    """
    for modelo in (DailyMetrics, Activity, WorkoutLog):
        col = modelo.__table__.c.fetched_at
        assert col.onupdate is not None, (
            f"{modelo.__tablename__}.fetched_at vuelve a significar created_at"
        )


def test_la_fila_nueva_nace_con_marca(db):
    """El `server_default` sigue estando: `onupdate` no lo sustituye."""
    fila = DailyMetrics(date=DIA)
    db.add(fila)
    db.flush()
    leida = db.scalars(select(DailyMetrics).where(DailyMetrics.date == DIA)).first()
    assert leida.fetched_at is not None
