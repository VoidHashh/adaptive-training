"""Las dos funciones que impiden que el panel escriba "día(s)".

POR QUÉ ESTO MERECE UN MÓDULO
-----------------------------
Porque el paréntesis de plural no es un descuido de estilo: es la firma de un
texto generado. Una frase que dice "solo hay 3 día(s) con las dos cosas
medidas" se lee como un log que se ha escapado a la pantalla, y en cuanto una
frase se lee como un log deja de leerse: el ojo la salta igual que salta un
identificador. El panel entero está escrito para que lo lea una persona que
acaba de levantarse, y esa persona no debería tener que deshacer mentalmente
un paréntesis para saber cuántos días hay.

`app/engine/signals.py` ya tomó esta decisión para el mensaje de Telegram -"Y
se concuerda el plural en vez de escribir 'sesion(es)'"- pero la resolvió a
mano, con un condicional escrito en el sitio. `encabezados.py` la volvió a
resolver a mano, con un `_cuantos` privado. Dos veces la misma regla en dos
módulos que no se conocen es exactamente el punto en el que hay que sacarla
fuera, antes de que aparezca la tercera y la cuarta y una de ellas escriba
"sesiónes".

POR QUÉ SE PASAN LAS DOS FORMAS ENTERAS Y NO UN SUFIJO
------------------------------------------------------
La tentación es `n + palabra + ("s" if n != 1 else "")`. En castellano eso
falla el primer día: "sesión" + "es" da "sesiónes", porque el plural mueve la
tilde -sesión/sesiones- y "vez" + "es" da "vezes". Lo cazó un test en
`portada.py`; a ojo se lee cuatro veces sin verlo. Así que las dos formas se
escriben enteras en la llamada y aquí no se conjuga nada: esto elige, no
construye.

POR QUÉ `abs`
-------------
Los desfases van de -3 a +3 días, y "se ADELANTA -1 días" es tan feo como
"1 días". El signo lo pone la frase que rodea al número; la concordancia solo
mira la magnitud.

EL GEMELO EN EL CLIENTE
-----------------------
`static/comun.js` tiene estas mismas dos funciones, con el nombre `plural()` y
`cuenta()`. No es duplicación evitable: parte de las frases del panel las
escribe el servidor -los `na` de `stats.py`, las lecturas de `auditoria.py`- y
parte las escribe el navegador con los números del payload. Lo que sí es
evitable es que las dos mitades de la misma pantalla concuerden distinto, así
que si aquí se cambia una regla, allí se cambia también.
"""

from __future__ import annotations


def plural(n: float, uno: str, varios: str) -> str:
    """La palabra que concuerda con `n`, sin el número delante.

    Para las frases donde el número ya está escrito o donde hay que concordar
    dos palabras con un solo recuento: "4 días prescritos" necesita elegir dos
    veces sobre el mismo `4`.
    """
    return uno if abs(n) == 1 else varios


def cuantos(n: float, uno: str, varios: str) -> str:
    """El número y su palabra ya concordados: `cuantos(1, "día", "días")` → "1 día"."""
    return f"{n} {plural(n, uno, varios)}"
