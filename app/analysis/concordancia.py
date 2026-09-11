"""Vistas 1 y 2: si lo que nota coincide con lo que mide el reloj, y con cuánto retraso.

Son la misma pregunta dos veces. La Vista 1 la hace el mismo día -¿el martes que
me sentí reventado tenía la HRV baja?- y la Vista 2 la hace corriendo la ventana
de -3 a +3 días, porque la respuesta interesante casi nunca cae en el cero: un
cuerpo que se rompe el lunes suele dar la cara el miércoles, y una percepción que
se adelanta es tan informativa como una que va por detrás.

Van juntas en un módulo porque comparten las parejas y comparten la trampa. Si el
emparejado se hiciera de dos maneras distintas, la Vista 1 y la Vista 2 podrían
discrepar en el desfase 0 y no habría forma de saber cuál miente.

LO QUE ESTE MÓDULO NO HACE
--------------------------
No decide nada. Ni una regla del motor mira aquí, ni debe. Estas dos vistas son
para MIRARLAS, y lo que se haga con lo que digan lo decide una persona editando
el `config.yaml`, no el sistema ajustándose solo. Un sistema que se recalibra con
su propia salida se persigue la cola.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.analysis import series as S
from app.analysis.stats import correlacion, emparejar, mejor_desfase

# Cuántos días mira por defecto. Coincide a propósito con la ventana del
# histórico de Garmin: pedir más sería pedir días que no existen, y pedir menos
# sería tirar a la basura el backfill que se gastó en traerlos.
DIAS_POR_DEFECTO = 180

# El barrido de la Vista 2, tal cual se pidió.
RANGO_DESFASE = range(-3, 4)


@dataclass(frozen=True)
class Par:
    """Una pareja que merece la pena mirar, y qué se espera de ella.

    `espera` es el signo que tendría la correlación SI la percepción y el reloj
    dijeran lo mismo. Casi siempre sale solo de los `sentido` de las dos series
    -si una es "alto peor" y la otra "alto mejor", coincidir significa correlación
    negativa-, y solo hay que escribirlo a mano cuando alguna de las dos es
    neutra. Tenerlo permite mandar al navegador la frase "esto va al revés de lo
    que debería", que es la única lectura de un signo que de verdad importa.
    """

    x: str
    y: str
    titulo: str
    espera: int | None = None  # 1, -1 o None = dedúcelo

    @property
    def signo_esperado(self) -> int | None:
        if self.espera is not None:
            return self.espera
        a, b = S.DEFINICIONES[self.x], S.DEFINICIONES[self.y]
        if a.sentido == "neutro" or b.sentido == "neutro":
            return None
        return 1 if a.sentido == b.sentido else -1


# Las parejas de la Vista 1. Son las cinco que se pidieron, con dos abiertas en
# dos: "sueño medido" son los minutos Y la nota, que no siempre van juntos -una
# noche larga y mala existe-, y "carga del entreno" son la bici Y la fuerza, que
# se miden en unidades distintas y no se pueden sumar. Abrirlas es lo contrario
# de las medias tintas: es no elegir por él cuál de las dos era la que valía.
PARES: tuple[Par, ...] = (
    Par("fatigue", "hrv", "Cansancio frente a variabilidad"),
    Par("fatigue", "rhr", "Cansancio frente a FC en reposo"),
    Par("sleep_quality", "sleep_min", "Sueño percibido frente a minutos dormidos"),
    Par("sleep_quality", "sleep_score", "Sueño percibido frente a nota de sueño"),
    Par("training_desire", "body_battery", "Ganas de entrenar frente a Body Battery"),
    # Neutras las dos, así que el signo va escrito: más carga debería ser más
    # esfuerzo percibido. Es la pareja más parecida a una identidad de todas, y
    # por eso es la mejor para detectar que algo está mal emparejado: si ESTA
    # sale plana, el que está roto es el desplazamiento de `yesterday_rpe`, no él.
    Par("yesterday_rpe", "carga_bici", "Esfuerzo percibido frente a carga de la bici", 1),
    Par(
        "yesterday_rpe",
        "volumen_fuerza",
        "Esfuerzo percibido frente a volumen de fuerza",
        1,
    ),
)


def _ventana(dias: int, hoy: date | None) -> tuple[date, date]:
    hoy = hoy or date.today()
    return hoy - timedelta(days=dias - 1), hoy


def _claves_de_la_vista() -> list[str]:
    """Deslizadores, métricas de Garmin y lo que haga falta para las parejas.

    Se calcula, no se escribe. Añadir una pareja nueva con una serie de entreno
    que no estuviera aquí dejaría la correlación calculada pero el gráfico sin
    una de sus dos líneas, y nadie se daría cuenta hasta abrirlo en el móvil.
    """
    claves = list(S.SLIDERS) + list(S.GARMIN)
    for p in PARES:
        for c in (p.x, p.y):
            if c not in claves:
                claves.append(c)
    return claves


def _lectura(par: Par, res: Any) -> str | None:
    """El signo traducido a una frase, o `None` si todavía no se puede decir nada."""
    if res.r is None:
        return None
    esperado = par.signo_esperado
    if esperado is None or abs(res.r) < 0.1:
        return "no se parecen lo bastante como para decir nada del sentido"
    va_como_toca = (res.r > 0) == (esperado > 0)
    fuerza = "claramente" if abs(res.r) >= 0.4 else "débilmente" if abs(res.r) < 0.25 else ""
    cola = "" if res.suficiente else " (muestra corta todavía)"
    if va_como_toca:
        return f"coincide {fuerza} con lo que marca el reloj{cola}".replace("  ", " ")
    return (
        f"va {fuerza} AL REVÉS de lo que marca el reloj{cola}".replace("  ", " ")
    )


def vista_concordancia(
    session: Session,
    *,
    dias: int = DIAS_POR_DEFECTO,
    hoy: date | None = None,
    metodo: str = "spearman",
) -> dict[str, Any]:
    """Vista 1: las series en un eje común y la correlación de cada pareja.

    Manda los valores CRUDOS y los normalizados a la vez. El navegador pinta los
    normalizados -que es lo único que permite superponer un 1-5 con una HRV en
    milisegundos- y enseña los crudos al tocar un punto. Mandar solo los
    normalizados obligaría a la PWA a deshacer la cuenta para enseñar el número
    de verdad, que es exactamente la cuenta que no puede hacer.
    """
    desde, hasta = _ventana(dias, hoy)
    cob = S.cobertura(session)

    crudas: dict[str, dict[date, float | None]] = {
        c: S.serie(session, c, desde, hasta, cob=cob) for c in _claves_de_la_vista()
    }

    salida_series = []
    for clave, valores in crudas.items():
        d = S.DEFINICIONES[clave]
        escala = S.normalizar(valores)
        puntos = [
            {
                "fecha": f.isoformat(),
                "valor": valores[f],
                "escala": escala[f],
            }
            for f in sorted(valores)
        ]
        salida_series.append(
            {
                **d.como_dict(),
                "n": sum(1 for v in valores.values() if v is not None),
                "puntos": puntos,
            }
        )

    salida_pares = []
    for par in PARES:
        pares = emparejar(crudas[par.x], crudas[par.y])
        res = correlacion(pares, metodo=metodo)
        salida_pares.append(
            {
                "x": par.x,
                "y": par.y,
                "etiqueta_x": S.DEFINICIONES[par.x].etiqueta,
                "etiqueta_y": S.DEFINICIONES[par.y].etiqueta,
                "titulo": par.titulo,
                "signo_esperado": par.signo_esperado,
                "lectura": _lectura(par, res),
                **res.como_dict(),
            }
        )

    return {
        "vista": "concordancia",
        "metodo": metodo,
        "ventana": {
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "dias": dias,
        },
        "cobertura": cob.como_dict(),
        "series": salida_series,
        "pares": salida_pares,
    }


def vista_desfase(
    session: Session,
    *,
    dias: int = DIAS_POR_DEFECTO,
    hoy: date | None = None,
    metodo: str = "spearman",
    rango: range = RANGO_DESFASE,
) -> dict[str, Any]:
    """Vista 2: los siete deslizadores contra las cinco métricas, de -3 a +3.

    La rejilla entera, treinta y cinco casillas, sin filtrar por "las que salen
    bien". Una casilla que sale a cero también es una respuesta -ese deslizador y
    esa métrica no tienen nada que ver, ni a la vez ni con retraso- y esconderla
    dejaría la rejilla llena de las que sobrevivieron por azar, que con treinta y
    cinco intentos son unas cuantas. Por eso viaja también la `p` de cada una: la
    forma de no engañarse con esta tabla es mirarla entera.
    """
    desde, hasta = _ventana(dias, hoy)
    cob = S.cobertura(session)

    # Se piden con margen: un desfase de -3 empareja el día D del deslizador con
    # el D-3 de la métrica, y si la métrica solo se hubiera leído desde `desde`,
    # los tres primeros días del barrido perderían pares que SÍ existen en la
    # base. El margen es lo que hace que la n de los extremos baje por falta de
    # datos de verdad y no por el corte de la consulta.
    margen = max(abs(min(rango)), abs(max(rango)))
    sliders = {
        c: S.serie(session, c, desde, hasta, cob=cob) for c in S.SLIDERS
    }
    metricas = {
        c: S.serie(
            session, c, desde - timedelta(days=margen), hasta + timedelta(days=margen), cob=cob
        )
        for c in S.GARMIN
    }

    rejilla = []
    for cx, sx in sliders.items():
        for cy, sy in metricas.items():
            df = mejor_desfase(sx, sy, rango=rango, metodo=metodo)
            rejilla.append(
                {
                    "x": cx,
                    "y": cy,
                    "etiqueta_x": S.DEFINICIONES[cx].etiqueta,
                    "etiqueta_y": S.DEFINICIONES[cy].etiqueta,
                    **df.como_dict(),
                }
            )

    return {
        "vista": "desfase",
        "metodo": metodo,
        "rango_desfase": [min(rango), max(rango)],
        "ventana": {
            "desde": desde.isoformat(),
            "hasta": hasta.isoformat(),
            "dias": dias,
        },
        "cobertura": cob.como_dict(),
        "convenio": (
            "desfase positivo = la percepción se adelanta al reloj; "
            "negativo = va por detrás"
        ),
        "rejilla": rejilla,
    }
