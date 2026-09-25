"""El formulario de después de entrenar: vocabulario, validación y cruce.

QUÉ SE PRUEBA AQUÍ Y POR QUÉ ASÍ
--------------------------------
Esta pieza tiene el mismo fallo silencioso que la capa de tendencia, por otro
camino: una respuesta que no está en el vocabulario se guardaría igual de bien
en una columna de texto, y saldría a CERO en todos los contadores para siempre
sin que nada avisara. El ejercicio que más guerra da aparecería con cero avisos,
y el hueco lo taparía la propia pieza que tenía que enseñarlo.

De ahí que casi todo aquí sea «esto revienta» y no «esto devuelve». Lo que se
prueba no es que el camino bueno funcione -eso se ve a la primera- sino que el
malo no pase de largo.

Y el cruce va por parejas, como el resto de la casa: el ejercicio que aparece
con una serie efectiva frente al que aparece SOLO con calentamiento, que es el
caso de al lado y el que un `if ejercicio in workout` daría por bueno.
"""

from __future__ import annotations

import pytest

from app.engine.feedback import (
    CANTIDAD,
    FALTA,
    HECHO,
    HECHO_SIN_APUNTAR,
    MOLESTIAS,
    RESPUESTAS_FALTA,
    RESPUESTAS_HECHO,
    TECNICA,
    FeedbackError,
    cruzar,
    molestaron,
    respuestas_de,
    sin_apuntar,
    validar_ejercicios,
    validar_eleccion,
    validar_mas_costoso,
)

SETS = {"source": "api"}


def plan(*claves: str) -> list[dict]:
    return [
        {"key": k, "name": k.replace("_", " ").capitalize(), "template_id": f"T-{k}"}
        for k in claves
    ]


def hecho_en_hevy(clave: str, *tipos: str, title: str | None = None) -> dict:
    return {
        "exercise_template_id": f"T-{clave}",
        "title": title or clave,
        "sets": [{"type": t, "reps": 10, "weight_kg": 20} for t in tipos],
    }


def workout(*ejercicios: dict) -> dict:
    return {"id": "w1", "exercises": list(ejercicios)}


# ---------------------------------------------------------------------------
# Los dos vocabularios
# ---------------------------------------------------------------------------


class TestVocabulario:
    def test_las_molestias_se_llaman_igual_en_los_dos(self):
        """El contador las busca por la MISMA cadena en los dos estados.

        Si «me molestó la espalda» se llamara `molestia_lumbar` en uno y
        `lumbar` en el otro, el recuento por ejercicio vería la mitad de los
        casos y no fallaría: daría un número más bajo, que es la forma de error
        que nadie detecta.
        """
        for m in MOLESTIAS:
            assert m in RESPUESTAS_HECHO, m
            assert m in RESPUESTAS_FALTA, m

    def test_lo_hice_y_no_lo_apunte_solo_existe_para_lo_que_falta(self):
        """No tiene sentido para algo que SÍ está apuntado.

        Y no es cosmético: es la única respuesta que cambia lo que hace el
        motor. Admitirla en un ejercicio presente convertiría un ejercicio
        contado en un ejercicio contado dos veces.
        """
        assert HECHO_SIN_APUNTAR in RESPUESTAS_FALTA
        assert HECHO_SIN_APUNTAR not in RESPUESTAS_HECHO

    def test_todas_las_respuestas_tienen_etiqueta_no_vacia(self):
        """La pantalla las pinta con esto y no lleva ninguna escrita a mano."""
        for tabla in (RESPUESTAS_HECHO, RESPUESTAS_FALTA):
            for clave, etiqueta in tabla.items():
                assert etiqueta.strip(), clave

    def test_un_estado_inventado_revienta(self):
        with pytest.raises(FeedbackError, match="estado desconocido"):
            respuestas_de("hecho_a_medias")


# ---------------------------------------------------------------------------
# La validación
# ---------------------------------------------------------------------------


class TestValidacion:
    def test_una_lista_buena_pasa_y_sale_limpia(self):
        out = validar_ejercicios([
            {"key": "a", "name": "A", "estado": HECHO, "respuesta": None},
            {"key": "b", "name": "B", "estado": FALTA, "respuesta": "sin_tiempo"},
        ])
        assert [e["key"] for e in out] == ["a", "b"]
        assert out[1]["respuesta"] == "sin_tiempo"

    def test_una_respuesta_del_otro_vocabulario_revienta(self):
        """El caso de al lado: la cadena existe, pero no para ese estado."""
        with pytest.raises(FeedbackError, match="no vale para un ejercicio"):
            validar_ejercicios([
                {"key": "a", "estado": HECHO, "respuesta": HECHO_SIN_APUNTAR},
            ])

    def test_una_respuesta_inventada_revienta(self):
        with pytest.raises(FeedbackError, match="no vale para un ejercicio"):
            validar_ejercicios([
                {"key": "a", "estado": HECHO, "respuesta": "me_dio_pereza"},
            ])

    def test_un_ejercicio_repetido_revienta(self):
        """Dos respuestas para lo mismo y el contador elegiría una sin criterio."""
        with pytest.raises(FeedbackError, match="aparece dos veces"):
            validar_ejercicios([
                {"key": "a", "estado": HECHO, "respuesta": None},
                {"key": "a", "estado": HECHO, "respuesta": "peso_alto"},
            ])

    def test_un_ejercicio_sin_clave_revienta(self):
        with pytest.raises(FeedbackError, match="sin `key`"):
            validar_ejercicios([{"estado": HECHO}])

    def test_lo_que_no_es_una_lista_revienta(self):
        with pytest.raises(FeedbackError, match="tienen que venir en una lista"):
            validar_ejercicios({"key": "a"})

    def test_nada_que_contestar_es_una_lista_vacia_y_no_un_error(self):
        assert validar_ejercicios(None) == []

    def test_un_ejercicio_que_falta_y_no_se_contesta_se_queda_sin_contestar(self):
        """NO se cuela como «me lo salté», que es una afirmación que nadie hizo.

        Es la misma disciplina que `sin_muestra` en la capa de tendencia: no
        haberlo mirado y haber mirado y no ver nada son cosas distintas, y
        fundirlas convierte el silencio en información.
        """
        (out,) = validar_ejercicios([{"key": "a", "estado": FALTA}])
        assert out["respuesta"] is None


# ---------------------------------------------------------------------------
# Lo que el motor y los contadores leen
# ---------------------------------------------------------------------------


class TestLectores:
    def test_sin_apuntar_saca_solo_los_que_se_hicieron_sin_registrar(self):
        out = sin_apuntar([
            {"key": "a", "estado": FALTA, "respuesta": HECHO_SIN_APUNTAR},
            {"key": "b", "estado": FALTA, "respuesta": "saltado"},
            {"key": "c", "estado": HECHO, "respuesta": None},
        ])
        assert out == {"a"}

    def test_molestaron_distingue_la_lumbar_de_lo_demas(self):
        """Con una hernia L4-L5 no son la misma noticia, y un booleano las funde."""
        out = molestaron([
            {"key": "peso_muerto", "estado": HECHO, "respuesta": "molestia_lumbar"},
            {"key": "press", "estado": HECHO, "respuesta": "molestia_otra"},
            {"key": "curl", "estado": HECHO, "respuesta": "peso_alto"},
        ])
        assert out == {
            "peso_muerto": "molestia_lumbar",
            "press": "molestia_otra",
        }

    def test_sin_nada_contestado_los_dos_lectores_estan_vacios(self):
        assert sin_apuntar(None) == set()
        assert molestaron([]) == {}


# ---------------------------------------------------------------------------
# El cruce con lo que hay en Hevy
# ---------------------------------------------------------------------------


class TestCruce:
    def test_un_ejercicio_con_serie_efectiva_esta_hecho(self):
        (e,) = cruzar(plan("sentadilla"), [workout(
            hecho_en_hevy("sentadilla", "warmup", "normal")
        )], SETS)
        assert e["estado"] == HECHO

    def test_el_mismo_con_SOLO_calentamiento_cuenta_como_que_falta(self):
        """La pareja. «Se abrió y se dejó» es una de las cuatro situaciones que
        este formulario existe para separar, y es la que más se parece a no
        haberlo hecho. Un `if ejercicio in workout` la daría por buena y dejaría
        al usuario sin el desplegable que explica por qué lo dejó."""
        (e,) = cruzar(plan("sentadilla"), [workout(
            hecho_en_hevy("sentadilla", "warmup")
        )], SETS)
        assert e["estado"] == FALTA

    def test_un_ejercicio_que_no_aparece_falta(self):
        (e,) = cruzar(plan("sentadilla"), [workout()], SETS)
        assert e["estado"] == FALTA

    def test_el_orden_es_el_del_plan(self):
        """Que es el orden en que se entrenan, y por tanto en el que se recuerdan."""
        out = cruzar(plan("a", "b", "c"), [workout()], SETS)
        assert [e["key"] for e in out] == ["a", "b", "c"]

    def test_lo_que_se_hizo_sin_estar_en_el_plan_tambien_sale(self):
        """Y con el nombre que le da Hevy, que es el único que existe.

        Dejarlos fuera por no tener clave nuestra perdería exactamente los
        entrenos que nadie más mira.
        """
        out = cruzar(plan("sentadilla"), [workout(
            hecho_en_hevy("sentadilla", "normal"),
            hecho_en_hevy("otra_cosa", "normal", title="Remo en polea"),
        )], SETS)
        assert len(out) == 2
        suelto = out[-1]
        assert suelto["key"] == "hevy:T-otra_cosa"
        assert suelto["name"] == "Remo en polea"
        assert suelto["estado"] == HECHO

    def test_un_suelto_con_solo_calentamiento_no_sale(self):
        """La pareja del anterior: sin una serie efectiva no hay nada que comentar."""
        out = cruzar(plan("sentadilla"), [workout(
            hecho_en_hevy("sentadilla", "normal"),
            hecho_en_hevy("otra_cosa", "warmup"),
        )], SETS)
        assert [e["key"] for e in out] == ["sentadilla"]

    @pytest.mark.parametrize("orden", ["calienta_primero", "calienta_despues"])
    def test_una_sesion_partida_en_dos_ratos_suma_las_series(self, orden):
        """Media docena en un rato y media en otro es el ejercicio hecho.

        LOS DOS ÓRDENES, y no es por simetría: con el rato bueno el ÚLTIMO, un
        `efectivas = efectivas` que pisara el valor anterior en vez de sumarlo
        daría el mismo resultado que sumar, y la mutación sobrevivía. Lo cazó el
        banco del 25/09/2026. Sumar significa que da igual en qué orden se
        apuntaron los dos ratos, y eso solo se prueba probando los dos.
        """
        calienta = workout(hecho_en_hevy("sentadilla", "warmup"))
        trabaja = workout(hecho_en_hevy("sentadilla", "normal"))
        ratos = (
            [calienta, trabaja] if orden == "calienta_primero" else [trabaja, calienta]
        )
        out = cruzar(plan("sentadilla"), ratos, SETS)
        assert out[0]["estado"] == HECHO

    def test_sin_entrenamiento_ninguno_todo_el_plan_falta(self):
        out = cruzar(plan("a", "b"), [], SETS)
        assert {e["estado"] for e in out} == {FALTA}

    def test_sin_plan_y_con_entreno_salen_los_sueltos(self):
        """Un entreno en un día sin plan -o sin decisión guardada- no se pierde."""
        out = cruzar([], [workout(hecho_en_hevy("x", "normal", title="Equis"))], SETS)
        assert len(out) == 1 and out[0]["name"] == "Equis"


# ---------------------------------------------------------------------------
# Las tres preguntas sobre la sesión entera
# ---------------------------------------------------------------------------


class TestElecciones:
    def test_no_contestar_es_valido_y_se_guarda_como_None(self):
        assert validar_eleccion(None, CANTIDAD, "cantidad") is None

    def test_una_cadena_vacia_NO_es_no_contestar(self):
        """`""` y `None` llegan por caminos distintos -un desplegable sin tocar
        y un campo ausente- y tratarlos igual es la forma de que un día se
        guarde `""` en la columna y ningún contador lo vea ni como respuesta ni
        como hueco."""
        with pytest.raises(FeedbackError):
            validar_eleccion("", CANTIDAD, "cantidad")

    @pytest.mark.parametrize("valor", ["corta", "justa", "larga"])
    def test_las_tres_de_cantidad_valen(self, valor):
        assert validar_eleccion(valor, CANTIDAD, "cantidad") == valor

    def test_una_cantidad_inventada_revienta(self):
        with pytest.raises(FeedbackError, match="cantidad"):
            validar_eleccion("regular", CANTIDAD, "cantidad")

    def test_una_tecnica_inventada_revienta(self):
        with pytest.raises(FeedbackError, match="tecnica"):
            validar_eleccion("perfecta", TECNICA, "tecnica")

    def test_el_que_mas_costo_tiene_que_ser_uno_de_los_de_hoy(self):
        """Sin esto se guardaria cualquier cadena y el cruce con el peso
        apuntado -que es para lo que existe la pregunta- buscaria un ejercicio
        que esa sesion no tuvo, y saldria vacio para siempre."""
        ejs = [{"key": "sentadilla"}, {"key": "press"}]
        assert validar_mas_costoso("press", ejs) == "press"
        with pytest.raises(FeedbackError, match="no está entre los ejercicios"):
            validar_mas_costoso("dominadas", ejs)

    def test_no_decir_cual_costo_mas_es_valido(self):
        assert validar_mas_costoso(None, [{"key": "a"}]) is None

    def test_los_dos_vocabularios_de_sesion_tienen_etiqueta(self):
        for tabla in (CANTIDAD, TECNICA):
            for clave, etiqueta in tabla.items():
                assert etiqueta.strip(), clave
