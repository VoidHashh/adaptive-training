"""Lo que convierte el histórico en respuestas, y solo en el servidor.

La PWA no calcula NADA. Ni una media, ni una correlación, ni un percentil: pide
un endpoint y pinta lo que llega. No es purismo de arquitectura, es que el
navegador es el peor sitio posible para que vivan estas cuentas -no se pueden
probar con la batería, no se pueden auditar desde el móvil, y el día que un
número salga raro no hay forma de saber si lo calculó mal el JavaScript o si es
que el dato era así-.

Todo lo de aquí devuelve `n` y la ventana usada, y cuando no puede calcular algo
devuelve el motivo escrito, nunca un cero.

Tres capas, y el orden importa:

  - `stats`, que no sabe de esta aplicación. Recibe dos diccionarios de fecha a
    número y devuelve correlaciones. Se puede probar con números de resultado
    conocido porque no toca la base de datos;
  - `series`, que va de SQLAlchemy a esos diccionarios. Aquí vive lo difícil de
    verdad, que no es estadística sino contabilidad: qué día le toca a cada
    número y qué significa que un día no tenga fila;
  - `concordancia`, que monta las dos anteriores en las vistas que se piden desde
    la PWA.
"""

from app.analysis.concordancia import (
    PARES,
    RANGO_DESFASE,
    Par,
    vista_concordancia,
    vista_desfase,
)
from app.analysis.impacto import (
    ADVERTENCIA_CONFUSION,
    DIAS_DESPUES,
    N_MINIMO_EXPUESTOS,
    Exposicion,
    contraste,
    ranking_ejercicios,
    vista_impacto,
)
from app.analysis.series import (
    DEFINICIONES,
    GARMIN,
    SLIDERS,
    Cobertura,
    Definicion,
    cobertura,
    comprobar_sliders,
    normalizar,
    serie,
)
from app.analysis.stats import (
    Desfase,
    N_MINIMO_CALCULABLE,
    N_MINIMO_FIABLE,
    Pares,
    Resultado,
    correlacion,
    emparejar,
    mejor_desfase,
    percentil,
    percentil_de,
    rangos,
)

__all__ = [
    "ADVERTENCIA_CONFUSION",
    "Cobertura",
    "DEFINICIONES",
    "DIAS_DESPUES",
    "Definicion",
    "Desfase",
    "Exposicion",
    "GARMIN",
    "N_MINIMO_CALCULABLE",
    "N_MINIMO_EXPUESTOS",
    "N_MINIMO_FIABLE",
    "PARES",
    "Par",
    "Pares",
    "RANGO_DESFASE",
    "Resultado",
    "SLIDERS",
    "cobertura",
    "comprobar_sliders",
    "contraste",
    "correlacion",
    "emparejar",
    "mejor_desfase",
    "normalizar",
    "percentil",
    "percentil_de",
    "ranking_ejercicios",
    "rangos",
    "serie",
    "vista_concordancia",
    "vista_desfase",
    "vista_impacto",
]
