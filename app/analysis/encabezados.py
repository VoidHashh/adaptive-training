"""Un encabezado por vista: qué contesta, y en qué estado está para contestarlo.

POR QUÉ NO ESTÁ ESCRITO A MANO
------------------------------
Porque un estado escrito a mano no envejece hacia "viejo", envejece hacia
"falso", y se sigue leyendo con la misma confianza. `docs/analisis.md` llevaba
meses diciendo "no implementada todavía" con cinco vistas en producción; la
tabla de `workout_log` decía "Sí" de cuatro columnas que no escribía nadie; el
validador certificaba una sección muerta. Tres veces el mismo fallo, y las tres
veces la frase que tenía que avisar del hueco era justo la que decía que no lo
había.

Así que aquí NADA describe el estado a mano. La parte fija de cada encabezado es
la PREGUNTA -qué contesta la vista, que no cambia mientras la vista sea la
misma-, y todo lo demás -si hay datos, cuántos, cuánto falta- sale de contar lo
que la vista acaba de devolver. Si mañana la vista devuelve la mitad, el
encabezado lo dice sin que nadie lo toque.

CADA VISTA SE CUENTA EN SU PROPIA MONEDA
----------------------------------------
No hay un "n" universal y no se finge que lo haya. En Concordancia la unidad es
el par de series que se ha podido correlacionar; en Auditoría es el día con
decisión; en Percepción es la sesión que se ha podido juzgar. Inventar un
denominador común daría un número comparable entre vistas y mentiroso en todas,
que es peor que cinco números que hay que leer cada uno con su etiqueta.

LOS TRES ESTADOS
----------------
`vacio` es que no se puede contestar todavía. `parcial` es que se puede
contestar a medias y conviene saberlo. `con_datos` es que la vista está haciendo
su trabajo. Que `parcial` exista es el punto: sin él, una vista con dos pares de
catorce se pintaría igual que una con los catorce, y la diferencia entre las dos
es justamente lo que habría que mirar antes de creerse nada.
"""

from __future__ import annotations

from typing import Any

from app.analysis.texto import cuantos

# Cuándo una vista pasa de "parcial" a "con_datos". No es un porcentaje de
# calidad: es cuánto de lo que la vista SABE mirar está mirando de verdad. Por
# debajo de la mitad, la vista está enseñando una esquina de su propio asunto y
# quien la lee tiene derecho a saberlo antes de sacar conclusiones.
FRACCION_COMPLETA = 0.5


def _lista(v: dict[str, Any], clave: str) -> list[Any]:
    """La lista que la vista DEBE traer. Revienta si no está.

    `v.get(clave) or []` daría exactamente el mismo `[]` en dos situaciones que
    no se parecen en nada: una vista que todavía no tiene datos, y una vista a
    la que alguien le ha renombrado la clave. La primera es el estado normal de
    un sistema que arranca; la segunda es un encabezado que dirá "Todavía no"
    para siempre, con toda la seguridad del mundo, mientras la vista debajo
    pinta sus treinta y cinco filas. Es el mismo fallo del `or 0.0` y de la
    tabla que decía "Sí" de columnas que no escribía nadie: el valor que se lee
    no es el valor que se usa.

    Así que la ausencia de la clave es un error y la lista vacía no lo es.
    """
    if clave not in v:
        raise KeyError(
            f"el encabezado esperaba la clave '{clave}' en el payload y no está; "
            "si la vista la ha renombrado, hay que renombrarla también aquí, "
            "porque si no el encabezado dirá 'Todavía no' con la vista llena"
        )
    valor = v[clave]
    return list(valor) if valor else []


def _estado(vivos: int, posibles: int) -> str:
    if not vivos:
        return "vacio"
    if posibles and vivos < posibles * FRACCION_COMPLETA:
        return "parcial"
    return "con_datos"


def _encabezado(
    *,
    pregunta: str,
    vivos: int,
    posibles: int,
    unidad: tuple[str, str],
    falta: str,
) -> dict[str, Any]:
    """Monta el encabezado. `falta` es lo que abriría la vista, no un reproche.

    La diferencia importa: "no has hecho check-ins" culpa al usuario de un hueco
    que muchas veces es del sistema, y este panel no está para regañar a nadie.
    "hace falta que se apunten sesiones" dice lo mismo sin sujeto acusado y
    además dice qué lo arregla.
    """
    estado = _estado(vivos, posibles)
    if estado == "vacio":
        resumen = f"Todavía no. Hace falta {falta}."
    elif estado == "parcial":
        resumen = (
            f"{cuantos(vivos, *unidad)} de {posibles}. "
            f"Lo que falta necesita {falta}."
        )
    else:
        resumen = f"{cuantos(vivos, *unidad)} de {posibles}."
    return {
        "pregunta": pregunta,
        "estado": estado,
        "resumen": resumen,
        "n": vivos,
        "de": posibles,
    }


# ---------------------------------------------------------------------------
# Una por vista. Reciben el payload ya montado y solo cuentan.
# ---------------------------------------------------------------------------


def de_concordancia(v: dict[str, Any]) -> dict[str, Any]:
    pares = _lista(v, "pares")
    return _encabezado(
        pregunta=(
            "¿Lo que notas por la mañana se parece a lo que mide el reloj ese "
            "mismo día?"
        ),
        vivos=sum(1 for p in pares if p.get("r") is not None),
        posibles=len(pares),
        unidad=("pareja calculada", "parejas calculadas"),
        falta="que coincidan el check-in y el dato del reloj el mismo día",
    )


def de_desfase(v: dict[str, Any]) -> dict[str, Any]:
    filas = _lista(v, "rejilla")
    return _encabezado(
        pregunta=(
            "¿Tu percepción se adelanta al reloj o va por detrás? Se mira de "
            "tres días antes a tres días después."
        ),
        vivos=sum(1 for f in filas if f.get("mejor_desfase") is not None),
        posibles=len(filas),
        unidad=("pareja con desfase", "parejas con desfase"),
        falta="más días seguidos con check-in y reloj a la vez",
    )


def de_impacto(v: dict[str, Any]) -> dict[str, Any]:
    """Cuenta CELDAS con coeficiente, no filas.

    Una fila con uno de sus tres retardos calculado no está calculada: está
    calculada un tercio. Contar filas daría un encabezado más halagüeño y menos
    cierto.
    """
    filas = _lista(v, "rejilla")
    celdas = [c for f in filas for c in _lista(f, "por_dia")]
    vivas = [c for c in celdas if c.get("r") is not None]
    e = _encabezado(
        pregunta=(
            "¿Qué te hace al cuerpo lo que entrenas, uno, dos y tres días "
            "después?"
        ),
        vivos=len(vivas),
        posibles=len(celdas),
        unidad=("relación calculada", "relaciones calculadas"),
        falta="que se apunten sesiones de fuerza y salidas con su clasificación",
    )
    # Y al lado, cuántas de las calculadas aguantan la corrección por mirar
    # tantas a la vez. Van las dos porque dicen cosas distintas: la primera es
    # cuánto ha podido mirar, la segunda cuánto ha encontrado. Enseñar solo la
    # segunda haría que una vista que no puede calcular nada se leyera igual que
    # una que ha calculado ciento treinta y cinco y no ha encontrado nada.
    e["fiables"] = sum(1 for c in vivas if c.get("significativa"))
    return e


def de_ranking_ejercicios(v: dict[str, Any]) -> dict[str, Any]:
    """Cuenta los ejercicios que de verdad están ORDENADOS, no los que salen.

    El ranking mete al final, sin esconderlos, los ejercicios cuyo retardo de
    ordenación no se ha podido calcular. Está bien que salgan -esconderlos sería
    peor-, pero no están rankeados: están listados. Contarlos como si lo
    estuvieran daría un encabezado que promete un orden que la mitad de la lista
    no tiene, y esta vista ya avisa en su propia advertencia de que sirve para
    saber por dónde mirar y no para sentenciar.
    """
    filas = _lista(v, "ranking")
    retardos = _lista(v, "retardos")
    primero = retardos[0] if retardos else 1
    ordenados = 0
    for f in filas:
        casilla = next(
            (c for c in _lista(f, "por_dia") if c.get("dias_despues") == primero),
            None,
        )
        if casilla is not None and casilla.get("r") is not None:
            ordenados += 1
    resp = v.get("respuesta") or {}
    etiqueta = resp.get("etiqueta") or resp.get("clave") or "la respuesta elegida"
    return _encabezado(
        pregunta=(
            f"De lo que haces en el gimnasio, ¿qué se nota después en "
            f"{str(etiqueta).lower()}?"
        ),
        vivos=ordenados,
        posibles=len(filas),
        unidad=("ejercicio ordenado", "ejercicios ordenados"),
        falta="repetir cada ejercicio en días sueltos, no siempre en la misma rutina",
    )


def de_auditoria(v: dict[str, Any]) -> dict[str, Any]:
    dias = _lista(v, "dias")
    reglas = _lista(v, "reglas")
    con_decision = sum(1 for d in dias if d.get("luz"))
    e = _encabezado(
        pregunta="¿Acierta el motor con el color del día, y qué regla lo decide?",
        vivos=con_decision,
        posibles=len(dias),
        unidad=("día con decisión", "días con decisión"),
        falta="que el motor corra cada mañana y guarde lo que decide",
    )
    # Una regla que no ha disparado nunca no es una regla rota, pero tampoco es
    # una regla probada, y el sitio para saberlo es este.
    e["reglas_declaradas"] = len(reglas)
    e["reglas_sin_estrenar"] = sum(
        1 for r in reglas if not r.get("veces_disparada")
    )
    return e


def de_umbral(v: dict[str, Any]) -> dict[str, Any]:
    """La moneda es la SALIDA con su antes y su después, no el día.

    El denominador son las salidas que se han podido medir más las que se han
    caído por faltarles un dato, y NO los días de la ventana anteriores a que
    hubiera bici. Esos van contados aparte en el payload: sumarlos aquí haría
    que pedir dos años en vez de seis meses empeorara el encabezado sin que
    hubiera pasado nada, y un estado que empeora por mirar más lejos es
    exactamente la clase de número que enseña a no hacer caso del encabezado.
    """
    if "salidas" not in v:
        raise KeyError(
            "el encabezado del umbral esperaba 'salidas' en el payload y no "
            "está; sin él diría 'Todavía no' con la vista llena"
        )
    s = v["salidas"] or {}
    medidas = int(s.get("medidas") or 0)
    perdidas = sum(int(x or 0) for x in (s.get("fuera") or {}).values())
    return _encabezado(
        pregunta=(
            "¿A partir de cuánta bici te pasa factura al día siguiente, y "
            "cuántos días dura la factura?"
        ),
        vivos=medidas,
        posibles=medidas + perdidas,
        unidad=("salida medida", "salidas medidas"),
        falta="salir en bici y dormir con el reloj la noche de antes y la de después",
    )


def de_calibracion(v: dict[str, Any]) -> dict[str, Any]:
    """La moneda es la previsualización EN LA QUE SE DIJO ALGO, no el desacuerdo.

    Contar desacuerdos daría un encabezado que empeora cuanto mejor va la cosa:
    un sistema perfectamente calibrado tendría cero, y el encabezado diría
    «Todavía no» para siempre con la vista funcionando. Lo que esta vista
    necesita para trabajar no son desacuerdos, son OPINIONES -síes y noes-, y ése
    es el número que dice si puede contestar.

    El denominador son todas las previsualizaciones de la ventana, así que el
    estado baja a `parcial` cuando se mira mucho y se opina poco. Es exactamente
    lo que hay que saber antes de leer un porcentaje calculado sobre las
    opinadas.
    """
    if "cuantas" not in v:
        raise KeyError(
            "el encabezado de calibración esperaba 'cuantas' en el payload y no "
            "está; sin él diría 'Todavía no' con la vista llena"
        )
    c = v["cuantas"] or {}
    e = _encabezado(
        pregunta=(
            "Cuando no compartes lo que decide el motor, ¿hacia qué lado tiras y "
            "en qué regla se concentra?"
        ),
        vivos=int(c.get("opinadas") or 0),
        posibles=int(c.get("total") or 0),
        unidad=(
            "previsualización en la que dijiste algo",
            "previsualizaciones en las que dijiste algo",
        ),
        falta="previsualizar y decir si compartes lo que enseña la tarjeta",
    )
    # Cuántos desacuerdos pueden juzgarse contra el resultado de la sesión, que
    # es lo único de esta vista que se calla hasta tener muestra. Va en el
    # encabezado para que se sepa CUÁNTO FALTA sin tener que bajar hasta el
    # bloque que está en silencio: un bloque callado sin contador al lado se lee
    # como un bloque roto.
    q = v.get("quien_acerto") or {}
    e["juicios"] = int(q.get("n") or 0)
    e["juicios_hacen_falta"] = int(q.get("hacen_falta") or 0)
    return e


def de_percepcion(v: dict[str, Any]) -> dict[str, Any]:
    if "contador" not in v:
        raise KeyError(
            "el encabezado de percepción esperaba 'contador' en el payload y no "
            "está; sin él diría 'Todavía no' con la vista llena"
        )
    c = v["contador"] or {}
    juzgadas = int(c.get("de") or 0)
    total = int(c.get("total_sesiones") or 0)
    return _encabezado(
        pregunta=(
            "Lo que la mañana prometía, ¿se parece a lo que de verdad salió?"
        ),
        vivos=juzgadas,
        posibles=total,
        unidad=("sesión que se ha podido juzgar", "sesiones que se han podido juzgar"),
        falta="cerrar sesiones con su resultado apuntado",
    )


# El registro es lo que convierte esto en una garantía en vez de en seis
# funciones sueltas: el test recorre la TABLA DE RUTAS de la aplicación -no una
# lista escrita a mano aquí al lado, que es lo que se quedaría vieja- y exige
# que cada endpoint de `/api/metrics/` tenga su entrada, menos la portada, que
# es su propio encabezado de arriba abajo. Una vista nueva sin encabezado no se
# queda sin él en silencio: rompe el test el día que se escribe.
POR_VISTA = {
    "concordancia": de_concordancia,
    "desfase": de_desfase,
    "impacto": de_impacto,
    "ranking-ejercicios": de_ranking_ejercicios,
    "auditoria": de_auditoria,
    "percepcion": de_percepcion,
    "umbral": de_umbral,
    "calibracion": de_calibracion,
}


def poner(nombre: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Añade `encabezado` al payload y lo devuelve. Revienta si no hay función.

    El `KeyError` es a propósito. La alternativa -devolver el payload tal cual-
    dejaría la vista nueva sin encabezado, funcionando perfectamente y sin que
    nadie se entere, que es el patrón que este panel entero está intentando
    dejar atrás.

    Por lo mismo se exige que el payload se identifique. `vista` NO se pone aquí
    a partir de `nombre` porque no son el mismo dato: `nombre` es el trozo de la
    URL -"ranking-ejercicios"- y `vista` es el identificador del payload
    -"ranking_ejercicios"-. Escribir uno encima del otro renombraría una vista
    en la respuesta sin que nadie hubiera tocado la respuesta. Así que solo se
    comprueba que esté, que es lo que hacía falta: dos vistas habían llegado
    hasta aquí sin declararse y nadie se enteró.
    """
    if "vista" not in payload:
        raise KeyError(
            f"el payload de '{nombre}' no dice qué vista es. Todas las demás "
            f"llevan 'vista', y la pantalla y los tests cuentan con ella"
        )
    payload["encabezado"] = POR_VISTA[nombre](payload)
    return payload
