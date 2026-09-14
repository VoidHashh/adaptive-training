"""Los tres colores del semáforo y cómo se llaman en castellano.

Un módulo de ocho líneas para dos constantes puede parecer exagerado. No lo es:
antes de existir, el proyecto tenía la misma tabla escrita CUATRO veces -en
`engine/message.py`, en `engine/tendencia.py`, en `analysis/auditoria.py` y en
`analysis/portada.py`- más una quinta escrita a mano en JavaScript, y el mismo
color se llamaba de cuatro formas distintas según qué fichero lo pintara. Con
cuatro copias no hay ningún sitio donde añadir un color; hay cuatro sitios donde
acordarse de añadirlo, que es otra cosa.

Y lo que se cuela por ese hueco no es un fallo ruidoso. En la pantalla de
auditoría salía «green: 0,0 % · amber: 100,0 % · red: 0,0 %» justo debajo de
unas fichas que ya ponían «0 verde · 1 ámbar · 0 rojo»: la PWA sabía traducir
la luz de cada regla, porque esa venía con su nombre al lado desde el servidor,
y no sabía traducir la del reparto, porque esa venía indexada por la clave y un
diccionario no tiene dónde colgar el nombre de su propia clave. Ninguna prueba
podía verlo: todas las claves existían y todos los valores eran cadenas
perfectamente válidas. Se encontró mirando la pantalla.

POR QUÉ EN MINÚSCULA, Y POR QUÉ ESA ES LA FORMA CANÓNICA
--------------------------------------------------------
Porque la minúscula es la única forma que se puede meter dentro de una frase
(«el semáforo está en ámbar», «100,0 % ámbar») y de la que salen las otras dos
sin inventarse nada: `VERDE` es `.upper()` y `Verde` es `.capitalize()`. Al
revés no funciona -de `VERDE` no sale `ámbar` sin saber dónde va la tilde- y de
hecho la tabla en mayúsculas de Telegram existía precisamente porque nadie
quiso arriesgarse a que `"ÁMBAR".lower()` hiciera algo raro.

Lo que NO sale de aquí por regla es el plural: «verde» → «verdes» pero «ámbar»
→ «ámbares», y pegar sufijos en castellano es el fallo que ya está documentado
entero en `app/analysis/texto.py`. Por eso `portada.py` sigue llevando su propia
tabla de plurales, escrita palabra a palabra. Es la misma decisión que allí: las
dos formas enteras, ninguna derivada.

POR QUÉ VIVE EN `engine` Y NO EN `analysis`
-------------------------------------------
Porque `engine/progression.py` escribe el motivo que luego se guarda en la base
-«el semáforo no está en verde (amber)», con la clave cruda dentro, que acababa
en la pantalla tal cual- y `app/analysis/__init__.py` importa `auditoria`, que
importa `progression`. Un `from app.analysis.texto import ...` dentro del motor
cierra el círculo y revienta el import. Este módulo no importa nada, así que
puede importarlo cualquiera.

EL ORDEN DE `LUCES` ES DATO
---------------------------
Verde, ámbar, rojo: de menos a más freno, que es como se lee un semáforo y como
se pintan las fichas del reparto. Es una tupla y no un conjunto porque el orden
de ese reparto sale de aquí; `tendencia.py` solo pregunta si una luz está
dentro, y para eso una tupla de tres sirve igual.
"""

from __future__ import annotations

LUCES: tuple[str, ...] = ("green", "amber", "red")

NOMBRE_LUZ: dict[str, str] = {"green": "verde", "amber": "ámbar", "red": "rojo"}


def nombre_luz(luz: str | None) -> str:
    """El nombre en castellano, o la clave tal cual si no es una de las tres.

    Devuelve la clave y no una cadena vacía ni un «—» a propósito: si algún día
    llega un color que no está en la tabla, lo que hay que ver en la pantalla es
    QUÉ color llegó. Un hueco silencioso ahí obligaría a abrir la base para
    averiguar qué se está mirando.
    """
    if not luz:
        return "sin luz"
    return NOMBRE_LUZ.get(luz, luz)
