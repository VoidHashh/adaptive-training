"""Mide el contrato real de la API de Hevy SIN TOCAR NADA DE LA CUENTA.

Ésta es LA herramienta y EL método para averiguar qué acepta y qué rechaza Hevy
cuando haga falta saberlo. El día que haya que responder «¿se puede mandar X en
el campo Y?», la respuesta se MIDE aquí; no se deduce de la documentación, no se
recuerda de la última vez y no se supone por analogía con otro campo.

EL MÉTODO: EL `routine_id` FANTASMA
------------------------------------
El PUT se manda contra un `routine_id` QUE NO EXISTE -un UUID v4 generado al
vuelo-. Eso lo vuelve seguro, y no por convención sino por una propiedad medida
de la API: **Hevy valida el cuerpo ANTES de buscar la rutina.** De ahí que sólo
haya dos desenlaces, y que ninguno de los dos pueda modificar la cuenta:

    400 + texto            ->  el cuerpo está mal, y el texto dice en qué.
                               Ésa es la medida que se busca.
    404 Routine not found  ->  el cuerpo pasó ENTERO y lo único que falló fue
                               el id. O sea: ACEPTADO.

Que un 404 se lea como «aceptado» es la parte contraintuitiva, y es justo la que
hace que esto funcione.

EL CONTROL NO ES OPCIONAL. La primera sonda manda un cuerpo BIEN FORMADO contra
el mismo id inexistente. Si el control da 404 y la sonda da 400, la diferencia
la produce el CUERPO y no el id, que es exactamente lo que hay que demostrar.
Sin el control un 400 podría venir de cualquier otra cosa -la clave, la ruta, un
id con formato inválido- y la tabla entera no probaría nada. Por eso, cuando se
filtra por línea de órdenes, hay que incluir siempre "CONTROL".

POR QUÉ NO SE PRUEBA CONTRA UNA RUTINA DE VERDAD, y no es por pereza: si Hevy
resultara ACEPTAR lo que se está sondeando, el PUT habría reemplazado la rutina
del usuario, porque el PUT de Hevy SUSTITUYE, no parchea. Una sonda que sólo es
segura si la hipótesis es cierta no es una sonda: es la escritura otra vez.

LÍMITES DE LO QUE ESTO PUEDE MEDIR
-----------------------------------
Distingue aceptado de rechazado, y nada más. NO puede decir qué VALOR queda
guardado cuando algo se acepta, porque nunca llega a guardarse nada. Por eso lo
de `Number([]) === 0` está dicho como lo que es -semántica de JavaScript, de la
que se deduce el valor- y no como una medida. Si algún día hace falta medir el
valor guardado de verdad, el camino es crear una rutina nueva de usar y tirar
con POST, escribir ahí, leerla y borrarla; lo que no se hace nunca es usar para
eso una rutina que le importe a alguien.

CÓMO SE AMPLÍA. Se añade una tupla a `SONDAS` y se lanza filtrando por su
etiqueta, con el control delante:

    python scripts/sondeo_contrato_hevy.py "CONTROL" "lo que sea"

Lo que NO se puede es ampliar la tabla de resultados a ojo. Lo que hay abajo
está medido, no recordado.

POR QUÉ HAY QUE MEDIRLO Y NO DEDUCIRLO
--------------------------------------
La escritura del 2026-09-14 falló y el código tiró el motivo a la basura:
`WriteResult` no traía el código HTTP, el runner guardaba `reason` (vacía) en
vez de `error`, y el log del contenedor se fue con el rebuild de esa tarde. O
sea que la causa más probable -`"notes": []`, un array JSON en un campo que el
GET devuelve siempre como cadena o `null`- es una HIPÓTESIS, y este proyecto ya
ha pagado caro tratar una hipótesis como un dato.

`tests/conftest.py` va a aprender a rechazar ese cuerpo igual que lo rechaza
Hevy. Antes de escribir esa regla hay que saber qué rechaza Hevy de verdad, con
qué código y con qué texto: un doble inventado a ojo es lo que dejó la reversión
rota seis meses con seis tests en verde.

CÓMO SE MIDE SIN ESCRIBIR NADA
------------------------------
Se hace el PUT contra un `routine_id` que NO EXISTE (un UUID v4 al azar). Solo
hay dos desenlaces y ninguno modifica la cuenta:

  - Si Hevy valida el cuerpo ANTES de buscar la rutina, contesta 400 y el texto
    dice exactamente qué campo está mal. Eso es la medida que se busca.
  - Si valida DESPUÉS, contesta 404 y no se aprende nada del tipo... pero
    tampoco se ha tocado nada.

El control es la primera sonda: el MISMO cuerpo bien formado contra el mismo id
inexistente. Si el control da 404 y la sonda con `notes: []` da 400, la
diferencia la produce el cuerpo y no el id, que es justo lo que hay que
demostrar. Sin el control, un 400 podría venir de cualquier otra cosa.

NO SE PRUEBA CONTRA LA RUTINA REAL, y no por pereza: si Hevy resultara ACEPTAR
`notes: []`, el PUT habría reemplazado la rutina del usuario (el PUT de Hevy
sustituye, no parchea). Una sonda que solo es segura si la hipótesis es cierta
no es una sonda, es la escritura otra vez.

LO QUE CONTESTÓ, MEDIDO EL 2026-09-14
--------------------------------------
El control dio **404 «Routine not found»**, así que Hevy VALIDA EL CUERPO ANTES
DE BUSCAR LA RUTINA. Todo lo que dio 400 lo dio por el cuerpo:

    400  notes = []                        Expected string, received array
    400  notes = ['a','b']                 Expected string, received array
    400  notes = 5                         Expected string, received number
    400  title = ['x']                     Expected string, received array
    400  title ausente                     Required
    400  notes del ejercicio = []          Expected string, received array
    400  reps = 'ocho'                     Expected number, received nan
    400  index en el ejercicio             Unrecognized key(s) in object: 'index'
    400  type = 'inventado'                Invalid set type
    400  type = []                         Expected string, received array
    404  rest_seconds = '90'               ACEPTADO
    404  reps = True                       ACEPTADO
    404  reps = '8'                        ACEPTADO
    404  weight_kg = []                    ACEPTADO

LOS SEIS CAMPOS NUMÉRICOS, UNO A UNO (ampliación del mismo día)
----------------------------------------------------------------
La tabla de arriba probaba dos campos numéricos y daba por hecho el resto. Son
seis, y no se valida NI UNO: los seis aceptan lista, nulo y cadena.

    404  rest_seconds = []      ACEPTADO      404  weight_kg = ''      ACEPTADO
    404  weight_kg = []         ACEPTADO      404  reps = ''           ACEPTADO
    404  distance_meters = []   ACEPTADO      404  weight_kg = '  '    ACEPTADO
    404  duration_seconds = []  ACEPTADO      404  reps = None         ACEPTADO
    404  custom_metric = []     ACEPTADO      404  rest_seconds = None ACEPTADO

Lo único que da 400 es `reps = 'ocho'`, y no porque se valide el tipo sino
porque `Number('ocho')` sale `NaN`.

LA CADENA VACÍA ES EL CASO PEOR DE TODO EL CONTRATO. La acepta Hevy Y la acepta
`_es_escalar` -es un `str` perfectamente válido-, y `Number('')` es 0. Era el
único camino conocido a un peso de CERO KILOS que no veía ninguna de las dos
capas, y un cero en un peso es un número plausible: no hay forma de distinguirlo
de una barra vacía puesta a propósito. Lo tapa `_exigir_numero`, que exige
número de verdad o nulo y rechaza las cadenas aunque Hevy las acepte.

Y EL TIPO DE SERIE SÍ ESTÁ VALIDADO, que es la excepción que confirma la
división: los campos de TEXTO los comprueba Hevy (`type` es un enumerado
cerrado, «Invalid set type»), los NUMÉRICOS no los comprueba nadie más que
nosotros.

Tres conclusiones, y las tres cambian código:

1. **El 2026-09-14 queda explicado y deja de ser una hipótesis.** El motor mandó
   `"notes": []` y esto es exactamente el 400 que contesta Hevy. La rutina no se
   tocó, que es lo que dice su `updated_at` del 2026-09-08.

2. **EL MENSAJE DE ERROR NO DICE QUÉ CAMPO ES.** «Expected string, received
   array» y nada más: ni la ruta, ni el nombre, ni el índice del ejercicio. O
   sea que aunque el código hubiera guardado el texto del 400 -que no lo
   guardaba, y ese es otro fallo ya arreglado-, habría dicho que algo de tipo
   array iba donde va texto, sin decir dónde. Por eso la comprobación local de
   `_exigir_texto` no sobra aunque Hevy valide: dice el campo.

3. **Y LOS CAMPOS NUMÉRICOS NO SE VALIDAN, SE COERCIONAN, QUE ES PEOR.**
   `weight_kg: []` no da error: pasa, porque en JavaScript `Number([])` es `0`.
   Un peso mal tipado se escribiría en la rutina como CERO KILOS sin una sola
   queja de nadie. Y `reps: True` pasa como 1 repetición, porque `Number(true)`
   es 1. Ahí Hevy no protege de nada: protege `_es_escalar`, que es local, es
   estricto y se ejecuta antes de que el cuerpo salga. Justo el sitio donde el
   proyecto ya sabe que hay que mirar: el valor que se lee no es el que se usa.

Si algún día hay que volver a medir, esto se ejecuta y ya está. Lo que NO se
puede es ampliar la tabla a ojo: lo de arriba está medido, no recordado.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import httpx  # noqa: E402

from app.settings import settings  # noqa: E402


# Un id que no existe. Se genera al vuelo para que no haya forma de que
# coincida con una rutina de verdad ni hoy ni dentro de un año.
ID_FANTASMA = str(uuid.uuid4())

# Un cuerpo mínimo y bien formado. Un solo ejercicio con una serie, todo con el
# tipo que la API documenta. Es el control: lo único que cambia entre sondas es
# el campo que se está midiendo.
EJERCICIO_OK = {
    "exercise_template_id": "79D0BB3A",  # Bench Press (Barbell), plantilla oficial
    "superset_id": None,
    "rest_seconds": 90,
    "notes": None,
    "sets": [
        {
            "type": "normal",
            "weight_kg": 60,
            "reps": 8,
            "distance_meters": None,
            "duration_seconds": None,
            "custom_metric": None,
        }
    ],
}


def cuerpo(**cambios) -> dict:
    base = {
        "title": "SONDA - no existe",
        "notes": None,
        "exercises": [json.loads(json.dumps(EJERCICIO_OK))],
    }
    base.update(cambios)
    return {"routine": base}


def con_ejercicio(**cambios) -> dict:
    """Un cuerpo bueno con el ÚNICO ejercicio alterado."""
    return cuerpo(exercises=[{**json.loads(json.dumps(EJERCICIO_OK)), **cambios}])


def con_serie(**cambios) -> dict:
    """Un cuerpo bueno con la ÚNICA serie alterada."""
    return con_ejercicio(sets=[{**EJERCICIO_OK["sets"][0], **cambios}])


SONDAS: list[tuple[str, dict]] = [
    # El control. Sin esto, un 400 no prueba nada.
    ("CONTROL: cuerpo bien formado, notes=None", cuerpo()),
    # La hipótesis del 2026-09-14, tal cual salió a la red.
    ("notes = [] (lo que mando el motor el 2026-09-14)", cuerpo(notes=[])),
    # La misma forma pero con contenido, por si la lista vacía se colara por
    # ser falsy y la llena no.
    ("notes = ['a', 'b']", cuerpo(notes=["a", "b"])),
    ("notes = 5 (numero en un campo de texto)", cuerpo(notes=5)),
    # Los otros sitios donde el mismo fallo de tipo podría repetirse mañana.
    ("title = ['x'] (lista en el titulo)", cuerpo(title=["x"])),
    ("title ausente", {"routine": {"notes": None, "exercises": [EJERCICIO_OK]}}),
    # ¿Es `title` NULABLE o solo obligatorio? No es lo mismo, y `_exigir_texto`
    # trataba los dos campos de texto igual: "texto o nada". Si `None` no vale
    # en el titulo, ese guardia tiene un agujero del tamaño de una escritura.
    ("title = None (nulo, no ausente)", cuerpo(title=None)),
    ("notes ausente del todo", {"routine": {"title": "SONDA - no existe",
                                            "exercises": [EJERCICIO_OK]}}),
    ("exercises ausente", {"routine": {"title": "SONDA - no existe",
                                       "notes": None}}),
    ("exercises = [] (rutina sin ejercicios)", cuerpo(exercises=[])),
    ("notes del ejercicio = []", con_ejercicio(notes=[])),
    ("rest_seconds = '90' (texto donde van segundos)", con_ejercicio(rest_seconds="90")),
    ("reps = True (un bool donde van repeticiones)", con_serie(reps=True)),
    ("reps = '8' (texto donde van repeticiones)", con_serie(reps="8")),
    ("reps = 'ocho' (texto no numerico en repeticiones)", con_serie(reps="ocho")),
    ("weight_kg = [] (lista donde va el peso)", con_serie(weight_kg=[])),
    # LA CADENA VACÍA, QUE ES LA PEOR DE TODAS
    # -----------------------------------------
    # `Number("")` es 0 en JavaScript, igual que `Number([])`. Si Hevy la acepta,
    # un peso vacío se escribe como CERO KILOS. Y a diferencia de la lista, una
    # cadena vacía SÍ pasa `_es_escalar` -es un `str`-, o sea que este es el
    # único camino conocido a un cero silencioso que el guardia de casa no ve.
    ("weight_kg = '' (cadena vacia donde va el peso)", con_serie(weight_kg="")),
    ("reps = '' (cadena vacia donde van repeticiones)", con_serie(reps="")),
    ("weight_kg = '  ' (espacios donde va el peso)", con_serie(weight_kg="  ")),
    # Los campos numéricos que NO se midieron la primera vez. Sin ellos la tabla
    # cubre dos de seis y el guardia se escribiría contra una muestra.
    ("rest_seconds = [] (lista donde van segundos)", con_ejercicio(rest_seconds=[])),
    ("distance_meters = [] (lista donde van metros)", con_serie(distance_meters=[])),
    ("duration_seconds = [] (lista donde van segundos)", con_serie(duration_seconds=[])),
    ("custom_metric = [] (lista donde va la metrica)", con_serie(custom_metric=[])),
    ("weight_kg = None (nulo donde va el peso)", con_serie(weight_kg=None)),
    ("reps = None (nulo donde van repeticiones)", con_serie(reps=None)),
    ("rest_seconds = None (nulo donde van segundos)", con_ejercicio(rest_seconds=None)),
    # El tipo de serie es TEXTO, y es el único campo de texto de la serie. Si
    # admite cualquier cadena, una errata escribe una serie de un tipo que no
    # existe; si está cerrado a una lista, hay que saberlo.
    ("type = 'inventado' (un tipo de serie que no existe)", con_serie(type="inventado")),
    ("type = [] (lista en el tipo de serie)", con_serie(type=[])),
    # El 400 que ya se conocía, para comparar la FORMA del mensaje.
    ("index en el ejercicio (el 400 ya conocido)", con_ejercicio(index=0)),
]


def main() -> int:
    s = settings
    if not s.hevy_api_key:
        print("falta HEVY_API_KEY en .env")
        return 2

    print(f"PUT /v1/routines/{ID_FANTASMA}  (este id NO existe: nada que romper)")
    print("=" * 78)

    # Sólo las sondas que se pidan por línea de órdenes, por subcadena. Se
    # sondea contra la API de otra persona: repetir la tabla entera para
    # confirmar un dato suelto es como se llega al 429, que además devuelve un
    # cuerpo vacío y no se distingue a simple vista de un fallo de tipo.
    filtro = sys.argv[1:]
    sondas = [
        (e, b) for e, b in SONDAS
        if not filtro or any(f.lower() in e.lower() for f in filtro)
    ]

    resultados = []
    with httpx.Client(base_url=s.hevy_api_base, timeout=30.0) as c:
        for i, (etiqueta, body) in enumerate(sondas):
            if i:
                # Un segundo entre sondas. La tabla entera de una tirada dio 429
                # la segunda vez que se lanzó.
                time.sleep(1.0)
            r = c.put(
                f"/v1/routines/{ID_FANTASMA}",
                headers={"api-key": s.hevy_api_key,
                         "Content-Type": "application/json"},
                json=body,
            )
            texto = (r.text or "").strip().replace("\n", " ")[:300]
            print(f"\n{etiqueta}")
            print(f"  -> {r.status_code}  {texto}")

            # UN 429 NO ES UNA MEDIDA, Y AQUI SE PARA
            # ---------------------------------------
            # Hevy limita el ritmo y contesta 429 con el cuerpo VACIO. En la
            # tabla eso sale como un codigo distinto del control -que es
            # justo lo que este guion define como "rechazado por el cuerpo"-
            # y sin texto que lo desmienta. O sea que un limite de ritmo se
            # lee igual que un fallo de tipo: la herramienta de medir
            # contando como dato lo que es su propia averia. Ese patron ya ha
            # costado bastante en este proyecto como para reproducirlo en el
            # guion que existe precisamente para no deducir nada.
            if r.status_code == 429:
                print(
                    f"\n  ABORTADO: Hevy esta limitando el ritmo (429). Lo de "
                    f"arriba NO son medidas y no se pueden usar. Espera un rato "
                    f"y repite, filtrando por la sonda que haga falta:\n"
                    f"      python scripts/sondeo_contrato_hevy.py \"{etiqueta[:30]}\""
                )
                return 3
            resultados.append((etiqueta, r.status_code, texto))

    print("\n" + "=" * 78)
    print("RESUMEN")
    for etiqueta, codigo, texto in resultados:
        print(f"  {codigo}  {etiqueta}")
    control = resultados[0][1]
    print(
        f"\nEl control dio {control}. Cualquier sonda con un codigo DISTINTO del "
        f"control esta siendo rechazada por el CUERPO, no por el id."
    )
    if filtro:
        print(
            "\nOJO: se ha filtrado, asi que la primera sonda de la lista NO es "
            "el control. Para leer la tabla entera hay que lanzarlo sin filtro."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
