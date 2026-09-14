"""Smoke test de bike_advisor contra el config real.

Recorre escenarios de historial x semáforo e imprime el punto de partida, de
dónde sale, la cadena de recortes y las notas. No es el test definitivo: es la
comprobación de que la cadena se comporta como dice el docstring.

LO QUE ESTE GUIÓN TIENE QUE ENSEÑAR AHORA
-----------------------------------------
Tres cosas, y las tres son consecuencia de haber quitado el calendario fijo:

1. **El día de la semana no pinta nada.** Antes este guión estaba organizado
   por sábado y domingo, y tenía una sección entera ("día que no es de finde")
   cuya salida esperada era `(no aplica: hoy es wednesday)`. Ahora la sección A
   recorre los siete días con el MISMO historial y tiene que dar siete veces lo
   mismo. Si alguna vez no coincide, ha vuelto a entrar el calendario por algún
   sitio y se ve sin leer código.

2. **El punto de partida se explica.** Se imprime `baseline_why`, que es la
   frase que justifica la banda. Una recomendación sin esa frase es un número
   caído del cielo.

3. **Los recortes siguen vacíos salvo el techo del semáforo.** La columna de
   recortes tiene que estar vacía en todo lo que no sea rojo o ámbar, y la de
   notas tiene que llevar el dato que antes justificaba el recorte. Por eso se
   imprimen por separado y con etiquetas distintas: si un hecho de contexto
   vuelve a aparecer en `recortes`, se ve de un vistazo.

Lo que este guión NO mide es si el esquema acierta. Eso es
`scripts/falsear_bici.py`, que corre contra datos reales y puede fallar.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.bike_advisor import recommend_bike
from app.engine.signals import ClassifiedRide, IntensityCount, Ride, Signals

CFG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")

HOY = date(2026, 9, 9)  # miércoles a propósito: el día que antes callaba

# Los mismos huecos que usan los tests, importados de allí en vez de copiados:
# con p40/p60 dan fronteras en 8 y 14 días, o sea suave por debajo de 8, intensa
# de 8 a 13 y media de 14 en adelante. Se importan porque la copia anterior de
# estos números se quedó describiendo el p25/p75 viejo y este guión imprimía
# encabezados falsos con toda naturalidad.
from tests.test_bike_advisor import DIAS_INTENSA, HUECOS, P_ALTO, P_BAJO  # noqa: E402


def ride(d: date, level: str) -> ClassifiedRide:
    return ClassifiedRide(
        ride=Ride(date=d, duration_s=5400),
        level=level,
        source="test",
        load=100.0,
        load_estimated=False,
    )


def historial(day: date, dias_desde: int, huecos: list[int] | None = None) -> list[ClassifiedRide]:
    """Salidas intensas encadenadas hacia atrás, la última hace `dias_desde`."""
    fechas = [day - timedelta(days=dias_desde)]
    for h in reversed(huecos if huecos is not None else HUECOS):
        fechas.append(fechas[-1] - timedelta(days=h))
    return [ride(f, "intensa") for f in sorted(fechas)]


def make_signals(
    day: date,
    rides: list[ClassifiedRide] | None = None,
    yesterday_level: str | None = None,
    conteo: IntensityCount | None = None,
    # Un día de la banda de 'intensa' a propósito, y no un número redondo. Las
    # secciones E y F enseñan que el recuento NO recorta y que el semáforo SÍ:
    # partiendo de 'suave' las dos saldrían iguales -no hay nada por debajo que
    # enseñar salvo descanso- y el guión parecería correcto sin demostrar nada.
    # Se parte del nivel más alto para que el recorte del semáforo se VEA.
    dias_desde: int = DIAS_INTENSA,
) -> Signals:
    return Signals(
        day=day,
        values={"yesterday_ride_level": yesterday_level},
        history={},
        adaptive={},
        notes=[],
        rides=historial(day, dias_desde) if rides is None else rides,
        intense_count=conteo,
    )


def cuenta(used: int, unknown: int = 0) -> IntensityCount:
    """La ventana de siete días que termina HOY, no un trozo de semana.

    Aquí ponía `week_start=HOY - timedelta(days=2)`, o sea el lunes de una
    semana que empezaba a contar el lunes. Con la ventana rodante hay que dar
    los dos extremos, y el de abajo tiene que ser `HOY - 6` para que el guión
    enseñe la misma frase que va a leer el usuario por la mañana.
    """
    return IntensityCount(
        used=used,
        detail=["test"],
        desde=HOY - timedelta(days=6),
        hasta=HOY,
        unknown=unknown,
    )


def show(title: str, sig: Signals, light: str) -> None:
    r = recommend_bike(CFG, sig, light)
    chain = " -> ".join(f"{a}>{b} ({why})" for a, b, why in r.downgrades) or "NINGUNO"
    print(f"\n{title}")
    print(f"  luz={light:5s} baseline={str(r.baseline):9s} final={r.level}")
    print(f"  punto de partida        : {r.baseline_why or '(sin base)'}")
    # Las dos frases van juntas y una debajo de otra a propósito: la de arriba
    # es la auditoría -con fecha y percentiles- y la de abajo es lo único que
    # llega al móvil. Si alguna vez la de abajo dice algo que la de arriba no
    # sostiene, se ve aquí sin abrir el código.
    print(f"  ...dicho en el mensaje  : {r.baseline_en_claro or '(nada)'}")
    print(f"  recortes (bajan nivel)  : {chain}")
    for n in r.texto_notas():
        print(f"  nota     (no baja nada) : {n}")
    print(f"  texto : {r.text() or '(bloque mudo, no ocupa sitio en el mensaje)'}")


print("=" * 78)
print("A. EL DÍA DE LA SEMANA NO PINTA NADA")
print("=" * 78)
print("   Mismo historial, los siete días. Las siete líneas tienen que decir")
print("   lo mismo. Aquí antes solo había sábado y domingo.")
niveles = set()
for i in range(7):
    d = HOY + timedelta(days=i)
    r = recommend_bike(CFG, make_signals(d), "green")
    niveles.add(r.level)
    print(f"   {d.strftime('%A'):<10} {d} -> {r.level}")
print(f"   => {'OK, uno solo' if len(niveles) == 1 else 'FALLO, ' + str(niveles)}")

# Este `FALLO` se imprimía y el guión salía con 0 de todas formas. Es la única
# comprobación de verdad que hay aquí -las demás secciones son para leerlas- y
# se tiraba a la basura justo después de calcularla. Se guarda y se cobra al
# final, que es donde puede pararle los pies a algo.
FALLOS: list[str] = []
if len(niveles) != 1:
    FALLOS.append(
        f"el día de la semana cambia la recomendación: {sorted(niveles)}. "
        f"Ha vuelto a entrar el calendario por algún sitio."
    )

print()
print("=" * 78)
print("B. LAS TRES BANDAS, que es lo que sustituye al calendario")
print("=" * 78)
print(f"   Fronteras en {P_BAJO} y {P_ALTO} días sobre los huecos {HUECOS}:")
print(f"   suave por debajo de {P_BAJO}, intensa de {P_BAJO} a {P_ALTO - 1}, "
      f"media de {P_ALTO} en adelante.")
for dd in (1, P_BAJO - 1, P_BAJO, P_ALTO - 1, P_ALTO, 40):
    show(f"{dd} días desde la última intensa", make_signals(HOY, dias_desde=dd), "green")

print()
print("=" * 78)
print("C. SIN BASE: se dice, no se inventa")
print("=" * 78)
show("sin ninguna salida", make_signals(HOY, rides=[]), "green")
show(
    "solo dos intensas (menos huecos que `min_gaps`)",
    make_signals(HOY, rides=historial(HOY, 7, huecos=[8])),
    "green",
)
show(
    "huecos todos iguales: los dos percentiles coinciden y la banda de "
    "'intensa' no existiría",
    make_signals(HOY, rides=historial(HOY, 7, huecos=[7, 7, 7, 7, 7])),
    "green",
)

print()
print("=" * 78)
print("D. Ayer se hizo una intensa: se dice, no se recorta")
print("=" * 78)
show(
    "ayer intensa",
    make_signals(HOY, dias_desde=1, yesterday_level="intensa"),
    "green",
)

print()
print("=" * 78)
print("E. Recuento de intensas alto: sigue saliendo lo que diga la banda")
print("=" * 78)
print("   (aquí antes había un 2/2 que bajaba el sábado a 'suave')")
print("   El recuento ya NO es nota de la bici: sale en su propia línea del")
print("   mensaje, porque se calcula después de decidir. Ver message.py.")
for n in (0, 2, 4, 7, 12):
    show(
        f"{n} sesiones intensas en los últimos 7 días",
        make_signals(HOY, conteo=cuenta(n)),
        "green",
    )

print()
print("=" * 78)
print("F. El techo del semáforo, que es el único recorte que queda")
print("=" * 78)
show("ámbar", make_signals(HOY), "amber")
show("rojo", make_signals(HOY), "red")
show(
    "ámbar con 9 intensas en los últimos 7 días",
    make_signals(HOY, conteo=cuenta(9)),
    "amber",
)

print()
print("=" * 78)
print("G. La salida de HOY no entra en su propio consejo")
print("=" * 78)
print("   Si contara, salir hoy en bici cambiaría el consejo de hoy a mitad de")
print("   día, y el mensaje de la mañana quedaría desmentido por la tarde.")
hist = historial(HOY, DIAS_INTENSA)
show(
    f"historial normal (última intensa hace {DIAS_INTENSA} días)",
    make_signals(HOY, rides=hist),
    "green",
)
show(
    "el mismo, más una intensa HOY",
    make_signals(HOY, rides=hist + [ride(HOY, "intensa")]),
    "green",
)

# La salida de hoy no puede cambiar el consejo de hoy, y esto es lo que lo
# comprueba en vez de dejarlo a la vista para que alguien lo compare a ojo.
sin_hoy = recommend_bike(CFG, make_signals(HOY, rides=hist), "green")
con_hoy = recommend_bike(
    CFG, make_signals(HOY, rides=hist + [ride(HOY, "intensa")]), "green"
)
if sin_hoy.level != con_hoy.level:
    FALLOS.append(
        f"la salida de HOY entra en su propio consejo: sin ella {sin_hoy.level}, "
        f"con ella {con_hoy.level}"
    )

print()
print("=" * 78)
print("TODO OK" if not FALLOS else f"{len(FALLOS)} FALLOS:")
for f in FALLOS:
    print(f"  - {f}")
print("=" * 78)
sys.exit(1 if FALLOS else 0)
