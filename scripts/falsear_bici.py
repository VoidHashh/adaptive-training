"""Intenta demostrar que el punto de partida de la bici es RUIDO.

POR QUÉ ESTE FICHERO EXISTE
---------------------------
`_baseline_gaps` sustituyó a un calendario fijo (`baseline_by_weekday`:
sábado=intensa, domingo=media) por tres bandas calculadas sobre los huecos
entre las salidas intensas del propio usuario. Un cambio así se defiende solo
si se mide, y medirlo significa intentar tumbarlo: un predictor que pierde
contra una constante no es un modelo, es ruido con más líneas de código.

La respuesta, medida sobre un año real de Garmin, es que EL ESQUEMA NUEVO
PIERDE COMO PREDICTOR. Acierta lo que el usuario acabó haciendo un 30% de las
veces; decir "suave" siempre acierta un 38%. Eso está escrito en `config.yaml`
y en el docstring de `_baseline_gaps`, y no se esconde aquí.

Lo que sí compra el esquema nuevo son dos bordes y una cosa:

  - CERO "intensa" al día siguiente de una intensa (el calendario daba 1).
  - CERO "intensa" volviendo de un parón de 30+ días (el calendario daba 5).
  - Habla todos los días. El calendario callaba 30 de 80 salidas reales, 6 de
    ellas intensas, y los miércoles no abría la boca nunca.

Con una hernia L4-L5 de por medio, esos dos ceros valen más que ocho puntos de
acierto sobre una etiqueta que el usuario elige, no que se le impone.

LO QUE ESTE GUIÓN VIGILA A PARTIR DE AHORA
------------------------------------------
Sale con código distinto de 0 si alguno de esos dos ceros deja de serlo. No
vigila el porcentaje de acierto: ya se sabe que es malo y no es lo que se
compró. Vigila lo único que justifica el cambio.

Y VIGILA LA TASA DE «INTENSA», QUE ES LA GUARDA QUE FALTABA
-----------------------------------------------------------
Esta tercera comprobación se añadió después de que este mismo guión diera el
visto bueno a unas bandas que recomendaban «intensa» el 61% de las veces
contra una tasa real del 21%. Los dos bordes seguían a cero y la tabla de
coincidencia no se movía, así que nada chilló. Una batería que no mide lo que
puede fallar aprueba igual que una que sí, y eso es peor que no tenerla:
parece cobertura.

El fallo era de diseño y merece quedar escrito aquí, porque el que lo repita
va a llegar por el mismo camino. Las bandas se definen por percentiles de los
HUECOS entre intensas, pero el contador de días desde la última sube de uno en
uno, así que el tiempo que se pasa dentro de una banda es proporcional a lo
ANCHA que sea en días, no a lo probable que sea. Con huecos dispersos, p25/p75
deja una banda central de 19 días de ancho contra una de 3: la banda que dice
«intensa» se come el eje del tiempo. Masa no es anchura.

Y llama a `_baseline_gaps` DE VERDAD, no a una copia de sus bandas escrita
aquí. Una batería de falsación que reimplementa lo que examina acaba
midiéndose a sí misma: el día que la producción cambie un `<` por un `<=`,
esto seguiría dando el visto bueno.

USO
---
    ./.venv/Scripts/python.exe -X utf8 scripts/falsear_bici.py
    ./.venv/Scripts/python.exe -X utf8 scripts/falsear_bici.py --garmin 400

Sin argumentos lee la caché de `data/cache/activities.json`, que es lo que el
sistema usa de verdad. Con `--garmin N` se conecta y pide N días, que es como
se hizo la medición original (400 días) cuando la caché aún era corta.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.bike_advisor import _baseline_gaps
from app.engine.signals import ClassifiedRide, Signals, classify_all
from app.integrations.activity_cache import RUTA_CACHE_SALIDAS, load_cached_rides

RAIZ = Path(__file__).resolve().parents[1]
CFG = load_config(RAIZ / "config.yaml")
CYC = CFG.raw["cycling"]
REC = CYC["recommendation"]
ORDEN = REC["intensity_order"]

# El calendario que se borró, reconstruido aquí para poder compararse contra
# él. Es la única copia que queda y vive en el guión que lo enterró, que es
# donde tiene que estar: en `config.yaml` sería una opción muerta.
CALENDARIO = {"saturday": "intensa", "sunday": "media"}


def salidas() -> list[ClassifiedRide]:
    """El histórico clasificado con las reglas reales de `cycling`."""
    if "--garmin" in sys.argv:
        dias = int(sys.argv[sys.argv.index("--garmin") + 1])
        from app.integrations.garmin import GarminClient
        from app.settings import settings

        cli = GarminClient(
            settings.garmin_email, settings.garmin_password, settings.garmin_token_dir
        )
        cli.connect()
        hoy = date.today()
        crudas = cli.rides(hoy - timedelta(days=dias), hoy)
        print(f"Fuente: Garmin, {dias} días.")
    else:
        cache = load_cached_rides(RUTA_CACHE_SALIDAS)
        if cache.error or not cache.rides:
            print(f"No hay caché utilizable: {cache.error or 'vacía'}", file=sys.stderr)
            raise SystemExit(2)
        crudas = cache.rides
        print(f"Fuente: caché en disco. {cache.describe()}")
    return sorted(classify_all(crudas, CYC), key=lambda r: r.date)


def _senales(dia: date, historial: list[ClassifiedRide]) -> Signals:
    """Señales mínimas: `_baseline_gaps` solo mira `day` y `rides`."""
    return Signals(
        day=dia,
        values={},
        history={},
        adaptive={},
        notes=[],
        rides=historial,
        weekend=None,
        intense_count=None,
    )


def main() -> int:
    todas = salidas()
    intensas = [r for r in todas if r.level == "intensa"]
    reparto = dict(Counter(r.level for r in todas))
    print(f"{len(todas)} salidas reales. Reparto: {reparto}")
    print(f"{len(intensas)} intensas. Ventana: {REC['baseline_from_gaps']['window_days']} días.\n")

    def nuevo(dia: date) -> str | None:
        # La producción, tal cual. Si un día devuelve `None` con un motivo, ese
        # día el sistema dice que no tiene base en vez de inventarse un nivel,
        # y aquí cuenta como "callado" igual que callaba el calendario.
        nivel, _why, _motivo = _baseline_gaps(REC, _senales(dia, todas), ORDEN)
        return nivel

    def viejo(dia: date) -> str | None:
        return CALENDARIO.get(dia.strftime("%A").lower())

    esquemas = {
        "viejo (sat=intensa, sun=media, entre semana nada)": viejo,
        "nuevo (3 bandas sobre huecos propios)": nuevo,
        "constante 'media'": lambda _d: "media",
        "constante 'suave'": lambda _d: "suave",
    }

    fechas_intensas = sorted({r.date for r in intensas})

    def dias_desde(dia: date) -> int | None:
        previas = [x for x in fechas_intensas if x < dia]
        return (dia - previas[-1]).days if previas else None

    print(f"{'esquema':<52} {'coincide':>9} {'dice INT':>9} {'callado':>8} "
          f"{'INT tras 1d':>12} {'INT tras parón':>15}")
    print("-" * 110)

    malos_del_nuevo = (0, 0)
    # La tasa de «intensa» del esquema nuevo y la tasa REAL del usuario medidas
    # sobre EXACTAMENTE las mismas salidas: las que tuvieron base. Comparar el
    # porcentaje del esquema (calculado sobre los días en que habla) contra la
    # tasa real del histórico entero (que incluye los días en que calla) sería
    # dividir por cosas distintas y llamarlo comparación.
    tasas_del_nuevo = (0, 0, 0)  # (dijo_intensa, fueron_intensa, n)
    for nombre, fn in esquemas.items():
        ok = mudo = malo_1d = malo_paron = n = dijo_int = fue_int = 0
        for r in todas:
            nivel = fn(r.date)
            if nivel is None:
                mudo += 1
                continue
            n += 1
            if nivel == r.level:
                ok += 1
            if nivel == "intensa":
                dijo_int += 1
            if r.level == "intensa":
                fue_int += 1
            dd = dias_desde(r.date)
            if nivel == "intensa" and dd is not None and dd <= 1:
                malo_1d += 1
            if nivel == "intensa" and dd is not None and dd >= 30:
                malo_paron += 1
        pct = f"{ok / n:.0%}" if n else "--"
        pct_int = f"{dijo_int / n:.0%}" if n else "--"
        print(f"{nombre:<52} {pct:>9} {pct_int:>9} {mudo:>8} "
              f"{malo_1d:>12} {malo_paron:>15}")
        if fn is nuevo:
            malos_del_nuevo = (malo_1d, malo_paron)
            tasas_del_nuevo = (dijo_int, fue_int, n)

    print("\n'callado'        = salidas reales en las que el sistema no dijo nada.")
    print("'dice INT'       = de las salidas en que habló, cuántas veces dijo intensa.")
    print("'INT tras 1d'    = dijo intensa al día siguiente de una intensa.")
    print("'INT tras parón' = dijo intensa con 30+ días sin ninguna intensa.")

    # Los dos "callado" no son la misma cosa y confundirlos sería tramposo.
    # El del calendario es estructural: entre semana no hablaba nunca, y eso
    # no se arregla con el tiempo. El del esquema nuevo es el arranque, porque
    # `min_gaps` exige un mínimo de huecos detrás y al principio del histórico
    # no los hay. Se comprueba en vez de afirmarse: si algún día apareciera un
    # mudo con histórico de sobra detrás, sería otro problema y hay que verlo.
    mudos = [r.date for r in todas if nuevo(r.date) is None]
    if mudos:
        primero = todas[0].date
        tarde = [d for d in mudos if (d - primero).days > 120]
        print(
            f"\nLos {len(mudos)} días callados del esquema nuevo van de "
            f"{mudos[0]} a {mudos[-1]}, sobre un histórico que empieza el "
            f"{primero}: es el arranque, no un agujero."
        )
        if tarde:
            print(
                f"  OJO: {len(tarde)} de ellos caen con 120+ días de histórico "
                f"detrás ({tarde[0]} … {tarde[-1]}). Eso ya no es arranque."
            )

    print()
    print("=" * 78)
    fallos: list[str] = []

    uno, paron = malos_del_nuevo
    if uno:
        fallos.append(f"{uno} veces 'intensa' al día siguiente de una intensa")
    if paron:
        fallos.append(f"{paron} veces 'intensa' volviendo de un parón de 30+ días")

    fallos += _guarda_de_tasa(*tasas_del_nuevo)

    if fallos:
        print("FALSADO. El esquema nuevo ha hecho algo de lo que no puede hacer:")
        for f in fallos:
            print(f"  - {f}")
        print("Eso era LO ÚNICO que compraba el cambio, porque como predictor")
        print("pierde contra la constante 'suave'. Si ya no lo compra, el esquema")
        print("no se sostiene y hay que revisarlo o quitarlo.")
        print("=" * 78)
        return 1
    print("Los dos bordes siguen a cero y la tasa de 'intensa' sigue pegada a la")
    print("real, que es lo que se compró.")
    print("Como PREDICTOR sigue perdiendo contra una constante, y eso no es un")
    print("fallo de esta ejecución: está asumido y escrito en `config.yaml`.")
    print("=" * 78)
    return 0


# Muestra mínima para que la tasa signifique algo. Con menos salidas con base
# que esto, un par de días cambian el porcentaje diez puntos y la guarda sería
# una moneda al aire. Se dice que no se ha medido en vez de aprobar por defecto:
# aprobar por falta de datos es justamente lo que hizo pasar a p25/p75.
MUESTRA_MINIMA = 20

# Las dos condiciones tienen que darse a la vez para declarar falsado. El factor
# solo, con tasas pequeñas, salta por nada (2% contra 1% es un x2 y son dos
# salidas). Los puntos solos no distinguen 45% contra 35% -que es mucho pero
# proporcionado- de 30% contra 5%, que es otra cosa.
FACTOR_MAXIMO = 2.0
PUNTOS_MINIMOS = 0.10


def _guarda_de_tasa(dijo_int: int, fue_int: int, n: int) -> list[str]:
    """¿Recomienda 'intensa' con una frecuencia parecida a la real del usuario?

    No es una medida de acierto: no mira SI coincidió el día, solo CUÁNTAS
    veces lo dijo. Un esquema puede fallar todos los días y aun así estar bien
    calibrado en frecuencia; lo que no puede es empujar a hacer series el triple
    de veces de las que se hacen, porque al otro lado hay una hernia L4-L5.

    Los dos esquemas que ya se tiraron habrían muerto aquí: la versión monótona
    decía intensa el 59% contra un 26% real, y p25/p75 el 61% contra un 21%.
    Ninguno de los dos movió las columnas de seguridad.
    """
    if n < MUESTRA_MINIMA:
        print(f"Tasa de 'intensa': NO MEDIDA. Solo {n} salidas con base y hacen "
              f"falta {MUESTRA_MINIMA}.")
        print("No se aprueba ni se suspende: se dice que esta guarda no ha mirado.")
        return []

    tasa = dijo_int / n
    real = fue_int / n
    print(f"Tasa de 'intensa': el esquema dice intensa el {tasa:.0%} de las "
          f"salidas con base ({dijo_int}/{n});")
    print(f"                   el usuario las hizo intensas el {real:.0%} "
          f"({fue_int}/{n}).")

    if tasa > real * FACTOR_MAXIMO and (tasa - real) >= PUNTOS_MINIMOS:
        return [
            f"dice 'intensa' el {tasa:.0%} de las veces contra un {real:.0%} real "
            f"(x{tasa / real:.1f} si la tasa real no es cero): eso ya no es "
            f"describir el ritmo del usuario, es empujarlo"
        ]

    # El otro extremo tiene su propio modo de fallo y no es simétrico: que la
    # banda de 'intensa' no se cumpla NUNCA es el mismo fallo mudo que la guarda
    # de `hi <= lo` vigila dentro de `_baseline_gaps`, pero por la puerta de
    # atrás: los percentiles pueden ser distintos y aun así dejar una banda tan
    # estrecha que el contador de días la salte sin pisarla. Callar aquí sería
    # dar por bueno un sistema que no vuelve a proponer una intensa en su vida.
    if dijo_int == 0 and fue_int:
        return [
            f"no dice 'intensa' ni una sola vez en {n} salidas con base, y el "
            f"usuario hizo {fue_int}: la banda de en medio existe en el config "
            f"pero el contador de días no la pisa nunca"
        ]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
