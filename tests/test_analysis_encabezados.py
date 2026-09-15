"""El encabezado de cada vista: que cuente lo que hay y que nadie se quede sin él.

Lo que se está protegiendo aquí son dos cosas distintas.

La primera es que el recuento sea VERDAD. Un encabezado que dice "135 de 396" y
se ha calculado mal es peor que no tenerlo: da una cifra, y una cifra se cree.
Por eso casi todas las pruebas de abajo montan un payload con una forma conocida
y exigen el número exacto, no que "haya" un número.

La segunda es que el registro no envejezca. `POR_VISTA` es un diccionario, y un
diccionario al lado de una lista de rutas se queda viejo en cuanto alguien añade
una ruta. Así que la prueba no compara contra una lista escrita a mano: recorre
la tabla de rutas de la aplicación de verdad. El día que alguien escriba la
sexta vista, esa prueba se pone roja sola.
"""

from __future__ import annotations

import pytest

from app.analysis import encabezados as E


# ---------------------------------------------------------------------------
# Constructores. Lo mínimo para que cada contador tenga algo que contar.
# ---------------------------------------------------------------------------


def casilla(r=None, dias_despues=1, significativa=False):
    return {"dias_despues": dias_despues, "r": r, "significativa": significativa}


# ---------------------------------------------------------------------------
# Los tres estados
# ---------------------------------------------------------------------------


def test_sin_nada_vivo_el_estado_es_vacio():
    assert E._estado(0, 10) == "vacio"


def test_sin_nada_vivo_y_sin_nada_posible_sigue_siendo_vacio():
    # Percepción en un sistema recién arrancado: 0 de 0. No es un error, es que
    # todavía no hay denominador.
    assert E._estado(0, 0) == "vacio"


def test_por_debajo_de_la_mitad_el_estado_es_parcial():
    assert E._estado(4, 10) == "parcial"


def test_justo_en_la_mitad_ya_no_es_parcial():
    # El umbral es `< posibles * FRACCION_COMPLETA`, así que la mitad exacta
    # cuenta como con_datos. Se fija aquí para que mover la constante se note.
    assert E._estado(5, 10) == "con_datos"


def test_con_todo_vivo_el_estado_es_con_datos():
    assert E._estado(10, 10) == "con_datos"


def test_un_solo_dato_de_muchos_es_parcial_y_no_con_datos():
    # Este es el caso que justifica que `parcial` exista: sin él, 1 de 14 se
    # pintaría igual que 14 de 14.
    assert E._estado(1, 14) == "parcial"


# ---------------------------------------------------------------------------
# El texto
# ---------------------------------------------------------------------------


def test_el_singular_no_dice_1_parejas():
    e = E._encabezado(
        pregunta="¿?",
        vivos=1,
        posibles=1,
        unidad=("pareja calculada", "parejas calculadas"),
        falta="datos",
    )
    assert "1 pareja calculada de 1" in e["resumen"]
    assert "1 parejas" not in e["resumen"]


def test_el_resumen_vacio_dice_que_hace_falta_y_no_culpa_a_nadie():
    e = E._encabezado(
        pregunta="¿?",
        vivos=0,
        posibles=9,
        unidad=("día con decisión", "días con decisión"),
        falta="que el motor corra cada mañana",
    )
    assert e["resumen"] == "Todavía no. Hace falta que el motor corra cada mañana."
    # Nada de "no has", "deberías", "te falta por hacer".
    assert "no has" not in e["resumen"].lower()


def test_el_resumen_parcial_lleva_el_numero_Y_lo_que_lo_abriria():
    e = E._encabezado(
        pregunta="¿?",
        vivos=2,
        posibles=14,
        unidad=("pareja calculada", "parejas calculadas"),
        falta="más días con las dos cosas medidas",
    )
    assert e["resumen"].startswith("2 parejas calculadas de 14.")
    assert "más días con las dos cosas medidas" in e["resumen"]


def test_el_resumen_completo_no_arrastra_la_coletilla_de_lo_que_falta():
    e = E._encabezado(
        pregunta="¿?",
        vivos=14,
        posibles=14,
        unidad=("pareja calculada", "parejas calculadas"),
        falta="más días",
    )
    assert e["resumen"] == "14 parejas calculadas de 14."


def test_el_encabezado_siempre_lleva_el_numero_crudo_al_lado_de_la_frase():
    # La frase es para leerla; `n` y `de` son para que el cliente pueda pintar
    # una barra o marcar una opción vacía sin tener que parsear castellano.
    e = E._encabezado(
        pregunta="¿?", vivos=3, posibles=7, unidad=("cosa", "cosas"), falta="x"
    )
    assert e["n"] == 3
    assert e["de"] == 7


# ---------------------------------------------------------------------------
# La clave que falta es un ERROR, no un cero
# ---------------------------------------------------------------------------


def test_una_clave_renombrada_revienta_en_vez_de_decir_todavia_no():
    """El fallo que este módulo entero está para no cometer.

    Si la vista renombra `pares`, `v.get("pares") or []` devolvería `[]` y el
    encabezado diría "Todavía no" para siempre, tranquilamente, encima de una
    vista llena de datos.

    OJO CON CÓMO SE COMPRUEBA ESTO. Una prueba que solo exigiera `KeyError`
    pasaría igual sin la guarda, porque `v[clave]` ya revienta solo: pasaría por
    el motivo equivocado y dejaría borrar la guarda sin que nada se pusiera
    rojo. Lo que la guarda añade no es la excepción, es la EXPLICACIÓN, así que
    es la explicación lo que hay que exigir aquí.
    """
    with pytest.raises(KeyError, match="si la vista la ha renombrado"):
        E.de_concordancia({"vista": "concordancia", "parejas": [{"r": 0.5}]})


def test_la_lista_vacia_de_verdad_NO_revienta():
    # La distinción es todo el asunto: la clave ausente es un error, la lista
    # vacía es el lunes por la mañana de un sistema que arranca.
    e = E.de_concordancia({"pares": []})
    assert e["estado"] == "vacio"


def test_la_lista_a_None_tampoco_revienta():
    e = E.de_concordancia({"pares": None})
    assert e["estado"] == "vacio"


def test_percepcion_sin_contador_revienta_explicando_por_que():
    # Mismo cuidado que arriba: se exige la frase, no la excepción, porque la
    # excepción saldría igual sin la guarda.
    with pytest.raises(KeyError, match="con la vista llena"):
        E.de_percepcion({"vista": "percepcion"})


# ---------------------------------------------------------------------------
# Cada vista cuenta en su propia moneda
# ---------------------------------------------------------------------------


def test_concordancia_cuenta_las_parejas_CALCULADAS_no_las_ofrecidas():
    v = {"pares": [{"r": 0.4}, {"r": None}, {"r": -0.2}, {"r": None}]}
    e = E.de_concordancia(v)
    assert (e["n"], e["de"]) == (2, 4)


def test_desfase_cuenta_las_filas_con_mejor_desfase():
    v = {"rejilla": [{"mejor_desfase": 1}, {"mejor_desfase": None}, {"mejor_desfase": 0}]}
    e = E.de_desfase(v)
    assert (e["n"], e["de"]) == (2, 3)


def test_un_desfase_de_cero_dias_cuenta_como_calculado():
    """Cero es un desfase, no un hueco.

    `if f.get("mejor_desfase")` lo habría tirado por falsy, y el desfase 0 -"va
    a la vez"- es justamente el resultado más frecuente y más interesante.
    """
    e = E.de_desfase({"rejilla": [{"mejor_desfase": 0}]})
    assert e["n"] == 1


def test_impacto_cuenta_CELDAS_y_no_filas():
    """Una fila con uno de tres retardos no está calculada: lo está un tercio."""
    v = {
        "rejilla": [
            {"por_dia": [casilla(0.4), casilla(None, 2), casilla(None, 3)]},
            {"por_dia": [casilla(0.1), casilla(0.2, 2), casilla(0.3, 3)]},
        ]
    }
    e = E.de_impacto(v)
    # Contando filas darían 2 de 2 y el encabezado diría "completo".
    assert (e["n"], e["de"]) == (4, 6)
    assert e["estado"] == "con_datos"


def test_impacto_separa_lo_que_ha_MIRADO_de_lo_que_ha_ENCONTRADO():
    """Los dos números dicen cosas distintas y por eso van los dos.

    Enseñar solo `fiables` haría que una vista incapaz de calcular nada se
    leyera igual que una que ha calculado 135 y no ha encontrado nada.
    """
    v = {
        "rejilla": [
            {
                "por_dia": [
                    casilla(0.4, significativa=True),
                    casilla(0.1, 2, significativa=False),
                    casilla(None, 3),
                ]
            }
        ]
    }
    e = E.de_impacto(v)
    assert e["n"] == 2
    assert e["fiables"] == 1


def test_una_correlacion_de_cero_cuenta_como_calculada():
    # r=0.0 es un resultado -"no se parecen en nada"-, no un hueco. Un filtro
    # por falsy lo habría perdido y el denominador mentiría hacia abajo.
    assert E.de_impacto({"rejilla": [{"por_dia": [casilla(0.0)]}]})["n"] == 1
    assert E.de_concordancia({"pares": [{"r": 0.0}]})["n"] == 1


def test_ranking_cuenta_los_ORDENADOS_no_los_listados():
    """Los incalculables salen en la lista, pero no están rankeados."""
    v = {
        "retardos": [1, 2, 3],
        "ranking": [
            {"por_dia": [casilla(0.5, 1), casilla(0.2, 2)]},
            {"por_dia": [casilla(None, 1), casilla(0.9, 2)]},
            {"por_dia": [casilla(-0.3, 1)]},
        ],
        "respuesta": {"clave": "lower_discomfort", "etiqueta": "Molestias lumbares"},
    }
    e = E.de_ranking_ejercicios(v)
    # El del medio tiene un r enorme a +2, pero el orden lo pone el +1 y ahí no
    # tiene nada: está en la lista, no en el ranking.
    assert (e["n"], e["de"]) == (2, 3)


def test_la_pregunta_del_ranking_nombra_la_respuesta_elegida():
    # El ranking es la única vista cuya pregunta cambia con un parámetro. Si la
    # pregunta se quedara fija diría "molestias lumbares" mirando la HRV.
    v = {
        "retardos": [1],
        "ranking": [],
        "respuesta": {"clave": "hrv", "etiqueta": "Variabilidad (HRV)"},
    }
    assert "variabilidad (hrv)" in E.de_ranking_ejercicios(v)["pregunta"]


def test_auditoria_cuenta_los_dias_CON_DECISION_no_los_dias_del_calendario():
    v = {
        "dias": [{"luz": "verde"}, {"luz": None}, {"luz": "roja"}, {"luz": None}],
        "reglas": [],
    }
    e = E.de_auditoria(v)
    assert (e["n"], e["de"]) == (2, 4)


def test_auditoria_dice_cuantas_reglas_no_se_han_estrenado():
    """Una regla que no ha disparado nunca no está rota, pero tampoco probada."""
    v = {
        "dias": [{"luz": "verde"}],
        "reglas": [
            {"veces_disparada": 3},
            {"veces_disparada": 0},
            {"veces_disparada": None},
            {},
        ],
    }
    e = E.de_auditoria(v)
    assert e["reglas_declaradas"] == 4
    assert e["reglas_sin_estrenar"] == 3


def test_percepcion_cuenta_las_sesiones_que_se_han_PODIDO_juzgar():
    v = {"contador": {"de": 3, "total_sesiones": 11}}
    e = E.de_percepcion(v)
    assert (e["n"], e["de"]) == (3, 11)
    assert e["estado"] == "parcial"


def test_percepcion_en_un_sistema_recien_arrancado_no_divide_por_cero():
    e = E.de_percepcion({"contador": {"de": 0, "total_sesiones": 0}})
    assert e["estado"] == "vacio"
    assert (e["n"], e["de"]) == (0, 0)


# ---------------------------------------------------------------------------
# `poner` y el registro
# ---------------------------------------------------------------------------


def test_poner_mete_el_encabezado_dentro_del_payload():
    p = E.poner("concordancia", {"vista": "concordancia", "pares": [{"r": 0.5}]})
    assert p["encabezado"]["n"] == 1
    assert p["pares"] == [{"r": 0.5}]


def test_un_payload_que_no_dice_que_vista_es_revienta():
    """La otra mitad del mismo portero, y la que faltaba.

    `poner` ya se negaba a colocar un encabezado que no supiera hacer. No se
    negaba a colocárselo a un payload que no se identifica, y por ese lado se
    colaron dos: `percepcion` y `umbral` llegaron a contestar 200 sin decir qué
    vista eran. Es el agujero de siempre -algo que falta y no da error- solo que
    mirando hacia el otro lado.

    No se arregla poniendo `vista` desde `nombre`, y merece la pena dejarlo
    escrito: son datos distintos. `nombre` es el trozo de URL
    -"ranking-ejercicios"- y `vista` el identificador del payload
    -"ranking_ejercicios"-. Copiando uno en el otro se renombraría una vista en
    la respuesta sin que nadie hubiera tocado la respuesta.
    """
    with pytest.raises(KeyError, match="vista"):
        E.poner("concordancia", {"pares": [{"r": 0.5}]})


def test_una_vista_sin_encabezado_revienta_en_vez_de_pasar_de_largo():
    """El `KeyError` es a propósito.

    Devolver el payload tal cual dejaría la vista nueva sin encabezado,
    funcionando perfectamente y sin que nadie se entere, que es el patrón que
    este panel entero está intentando dejar atrás.
    """
    with pytest.raises(KeyError):
        E.poner("vista_que_alguien_acaba_de_escribir", {})


def test_todas_las_rutas_de_metrics_tienen_encabezado():
    """Recorre la TABLA DE RUTAS de verdad, no una lista escrita aquí al lado.

    Una lista escrita a mano en esta prueba tendría exactamente el problema que
    la prueba intenta evitar: se quedaría vieja en silencio.
    """
    from app.api import app

    rutas = {
        r.path.rsplit("/", 1)[-1]
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/metrics/")
    }
    # La portada no lleva encabezado de estos porque ES un encabezado entera.
    esperadas = rutas - {"portada"}

    assert esperadas, "no se ha encontrado ninguna ruta de métricas: ¿ha cambiado el prefijo?"
    sin_encabezado = esperadas - set(E.POR_VISTA)
    assert not sin_encabezado, (
        f"estas vistas no tienen encabezado: {sorted(sin_encabezado)}. "
        "Una vista sin encabezado se queda sin explicar su propio vacío."
    )


def test_el_registro_no_tiene_entradas_muertas():
    """Conéctalas o bórralas, pero que no quede ninguna.

    Una función de encabezado para una vista que ya no existe no hace daño, pero
    es una respuesta a una pregunta que nadie hace, y dentro de seis meses nadie
    sabrá si se puede borrar.
    """
    from app.api import app

    rutas = {
        r.path.rsplit("/", 1)[-1]
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/metrics/")
    }
    sobran = set(E.POR_VISTA) - rutas
    assert not sobran, f"encabezados sin vista: {sorted(sobran)}"


def test_la_portada_no_se_cuela_en_el_registro():
    # Si algún día alguien le pone encabezado a la portada, la portada tendría
    # un encabezado dentro de un encabezado. La prueba de arriba la exime; esta
    # comprueba que la exención sigue haciendo falta.
    assert "portada" not in E.POR_VISTA


def test_cada_vista_viva_trae_su_encabezado_por_la_API():
    """De extremo a extremo, contra la aplicación montada.

    Las de arriba prueban los contadores con payloads de mentira. Esta prueba lo
    que comprueba es que `poner` está de verdad ENCHUFADO en cada endpoint: se
    puede tener el módulo perfecto y no haberlo llamado en ningún sitio.
    """
    from fastapi.testclient import TestClient

    from app.api import app

    with TestClient(app) as c:
        for nombre in E.POR_VISTA:
            r = c.get(f"/api/metrics/{nombre}")
            assert r.status_code == 200, f"{nombre}: {r.status_code}"
            e = r.json().get("encabezado")
            assert e is not None, f"{nombre} no trae encabezado"
            assert e["pregunta"].strip()
            assert e["resumen"].strip()
            assert e["estado"] in {"vacio", "parcial", "con_datos"}


def test_ninguna_pregunta_se_repite_entre_vistas():
    # Dos vistas con la misma pregunta serían dos vistas de más o una pregunta
    # mal escrita, y las dos cosas conviene saberlas.
    from fastapi.testclient import TestClient

    from app.api import app

    with TestClient(app) as c:
        vistas = [
            c.get(f"/api/metrics/{n}").json()["encabezado"]["pregunta"]
            for n in E.POR_VISTA
        ]
    assert len(set(vistas)) == len(vistas)
