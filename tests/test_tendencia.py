"""La capa de tendencia: lo que ninguna regla del semáforo puede ver.

Qué se prueba aquí y por qué de esta manera
-------------------------------------------
Esta capa tiene una propiedad incómoda: **se equivoca en silencio**. Una regla
del semáforo que falla se nota el mismo día, porque el color sale mal. Un
detector de tendencia que no dispara nunca no falla: se calla, y callarse es
justo lo que hace cuando no hay nada que contar. Los dos casos se leen igual
desde fuera.

De ahí la forma de estos tests. No basta con comprobar que un detector dispara
cuando debe; hay que comprobar **que no dispara cuando no debe y que sí dispara
en el caso justo de al lado**, porque un umbral mal puesto pasa los primeros y
falla solo los segundos. Casi todos los tests de racha y motivo van por parejas:
el caso que dispara y su vecino que no.

Y se prueba contra el `config.yaml` de verdad (racha≥5, motivo≥4 semanas,
30d/90d, Δ15pp). Un config de juguete aquí sería peor que inútil: los cinco
umbrales de `trend` son ABSOLUTOS por decisión explícita -está razonado en el
YAML- y lo único que los sujeta es el histórico y esta batería.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from app.engine.tendencia import (
    NEUTRAS_SEGUIDAS_MAX,
    DecisionDia,
    TendenciaError,
    evaluar_tendencia,
    senales_de_regla,
    tema_de_regla,
)

# Un lunes, para que las semanas ISO cuadren sin tener que pensarlas.
LUNES = date(2026, 6, 1)


# ---------------------------------------------------------------------------
# Constructores
# ---------------------------------------------------------------------------


def serie(hasta: date, dias: int, luz: str = "green", regla: str | None = None):
    """`dias` días consecutivos que terminan en `hasta`, todos iguales."""
    return [
        DecisionDia(hasta - timedelta(days=i), luz, regla)
        for i in range(dias - 1, -1, -1)
    ]


def racha_de(hasta: date, largo: int, regla: str, colchon: int = 40):
    """Un colchón de verdes y, pegada al final, una racha de ámbar.

    El colchón existe para que el detector de racha no se quede sin histórico
    antes de tiempo: sin él, "no dispara" podría ser "no hay con qué".
    """
    inicio_racha = hasta - timedelta(days=largo - 1)
    verdes = serie(inicio_racha - timedelta(days=1), colchon, "green")
    return verdes + serie(hasta, largo, "amber", regla)


def semanas(fin_domingo: date, reglas_por_semana: list[str | None], dias: int = 7):
    """Semanas ISO completas, de la más antigua a la más reciente.

    `reglas_por_semana` va del pasado al presente. `None` marca una semana
    neutra: verde entera, sin ninguna regla que domine.
    """
    out: list[DecisionDia] = []
    n = len(reglas_por_semana)
    for idx, regla in enumerate(reglas_por_semana):
        domingo = fin_domingo - timedelta(days=7 * (n - 1 - idx))
        lunes = domingo - timedelta(days=6)
        for d in range(dias):
            dia = lunes + timedelta(days=d)
            if regla is None:
                out.append(DecisionDia(dia, "green", None))
            else:
                out.append(DecisionDia(dia, "amber", regla))
    return out


def textos(t) -> str:
    return " | ".join(a.texto for a in t.avisos)


def tipos(t) -> set[str]:
    return {a.tipo for a in t.avisos}


def na(t) -> set[str]:
    return {s.tipo for s in t.sin_muestra}


# ---------------------------------------------------------------------------
# Errores duros: lo que no puede pasar en silencio
# ---------------------------------------------------------------------------


class TestErroresDuros:
    """Todo lo que aquí revienta, revienta a propósito.

    En un sistema que decide solo y manda un mensaje a las 7 de la mañana, un
    dato imposible que se descarta sin avisar es peor que una excepción: la
    excepción se ve, el descarte se convierte en una tendencia calculada sobre
    un histórico que no es el que hubo.
    """

    def test_sin_seccion_trend_es_error(self, cfg_copia):
        del cfg_copia.raw["trend"]
        with pytest.raises(TendenciaError, match="falta la sección 'trend'"):
            evaluar_tendencia(cfg_copia, LUNES, [])

    def test_semaforo_desconocido_es_error(self, cfg):
        with pytest.raises(TendenciaError, match="semáforo desconocido"):
            evaluar_tendencia(cfg, LUNES, [DecisionDia(LUNES, "naranja")])

    def test_dia_repetido_es_error(self, cfg):
        """Dos filas para el mismo día significa que nadie resolvió `is_current`."""
        with pytest.raises(TendenciaError, match="aparece dos veces"):
            evaluar_tendencia(
                cfg, LUNES,
                [DecisionDia(LUNES, "green"), DecisionDia(LUNES, "amber")],
            )

    def test_dia_futuro_es_error(self, cfg):
        with pytest.raises(TendenciaError, match="no puede mirar hacia adelante"):
            evaluar_tendencia(
                cfg, LUNES, [DecisionDia(LUNES + timedelta(days=1), "green")]
            )

    def test_el_dia_evaluado_si_vale(self, cfg):
        """El llamante mete la decisión de HOY, que aún no está en la base."""
        t = evaluar_tendencia(cfg, LUNES, [DecisionDia(LUNES, "green")])
        assert t.n == 1


# ---------------------------------------------------------------------------
# Apagada y sin muestra
# ---------------------------------------------------------------------------


class TestApagadaYSinMuestra:
    def test_enabled_false_no_dice_nada(self, cfg_copia):
        """Apagada se calla del todo: ni avisos ni N/A ni 'sin novedad'."""
        cfg_copia.raw["trend"]["enabled"] = False
        t = evaluar_tendencia(cfg_copia, LUNES, serie(LUNES, 120, "amber", "sueno_corto"))
        assert t.activa is False
        assert t.lineas() == []
        assert t.avisos == []

    def test_historico_vacio_lo_dice(self, cfg):
        t = evaluar_tendencia(cfg, LUNES, [])
        assert na(t) == {"todos"}
        assert t.lineas() == [
            "Tendencia: sin muestra para todos (no hay ni una decisión previa)"
        ]

    def test_sin_muestra_no_es_todo_bien(self, cfg):
        """La diferencia que justifica la capa entera.

        Con un día de histórico no hay racha, pero decir "sin novedad" sería
        afirmar que se ha mirado. No se ha mirado: no hay con qué.
        """
        t = evaluar_tendencia(cfg, LUNES, [DecisionDia(LUNES, "amber", "sueno_corto")])
        assert t.avisos == []
        assert na(t) == {"racha", "motivo", "ventana"}
        assert "sin novedad" not in " ".join(t.lineas())


# ---------------------------------------------------------------------------
# Detector 1 — racha
# ---------------------------------------------------------------------------


class TestRacha:
    def test_un_dia_por_debajo_no_dispara(self, cfg):
        t = evaluar_tendencia(cfg, LUNES, racha_de(LUNES, 4, "hrv_baja_1d"))
        assert "racha" not in tipos(t)

    def test_el_umbral_justo_dispara(self, cfg):
        t = evaluar_tendencia(cfg, LUNES, racha_de(LUNES, 5, "hrv_baja_1d"))
        assert "racha" in tipos(t)
        assert "5 días seguidos sin un verde" in textos(t)

    def test_un_verde_la_corta(self, cfg):
        """Verde en medio: la racha cuenta desde el verde, no desde antes."""
        dec = racha_de(LUNES, 8, "hrv_baja_1d")
        corte = LUNES - timedelta(days=3)
        dec = [d if d.day != corte else DecisionDia(corte, "green") for d in dec]
        t = evaluar_tendencia(cfg, LUNES, dec)
        assert "racha" not in tipos(t)

    def test_rojo_cuenta_como_no_verde(self, cfg):
        """La racha cuenta días SIN verde, no días ámbar.

        Cinco rojos seguidos son la peor racha posible y sería absurdo que no la
        viera por mirar solo el ámbar.
        """
        rojos = [
            DecisionDia(d.day, "red", d.trigger_rule) if d.light == "amber" else d
            for d in racha_de(LUNES, 5, "hrv_hundida_2d")
        ]
        assert "racha" in tipos(evaluar_tendencia(cfg, LUNES, rojos))

    def test_un_hueco_no_rompe_la_racha_y_se_nombra(self, cfg):
        """Un apagón del sistema no es un día verde.

        Si el contenedor estuvo caído el miércoles, el jueves no puede empezar a
        contar de cero: no hubo un buen día, hubo un día sin datos. Pero el aviso
        tiene que decirlo, porque "9 días seguidos" con dos huecos dentro no es
        lo mismo que nueve días medidos.
        """
        dec = [d for d in racha_de(LUNES, 9, "sueno_corto")
               if d.day != LUNES - timedelta(days=4)]
        t = evaluar_tendencia(cfg, LUNES, dec)
        assert "racha" in tipos(t)
        # Ocho días medidos sobre nueve de calendario, y el aviso dice las dos
        # cosas: el tramo entero y cuántos días de dentro no se midieron.
        assert "8 días seguidos" in textos(t)
        assert "1 sin decisión por medio" in textos(t)

    def test_un_hueco_fuera_de_la_racha_no_se_cuenta(self, cfg):
        """El fallo que tuvo esta función y por el que existe este test.

        Caminando hacia atrás, los huecos se encontraban ANTES de saber si la
        racha seguía viva. Un hueco pegado al verde que la termina cae fuera de
        la racha y contarlo inflaba el aviso con un apagón que no le tocaba.
        """
        dec = racha_de(LUNES, 5, "sueno_corto")
        antes = LUNES - timedelta(days=5)          # el verde que corta
        dec = [d for d in dec if d.day != antes - timedelta(days=0)]
        t = evaluar_tendencia(cfg, LUNES, dec)
        assert "racha" in tipos(t)
        assert "5 días seguidos" in textos(t)
        assert "hueco" not in textos(t)

    def test_nombra_el_tema_que_manda(self, cfg):
        t = evaluar_tendencia(cfg, LUNES, racha_de(LUNES, 6, "sueno_corto"))
        assert "Manda el sueño" in textos(t)

    def _empate_de_dos(self):
        """Seis días sin verde: tres los manda el sueño y tres el HRV."""
        dec = racha_de(LUNES, 6, "sueno_corto")
        return [
            DecisionDia(d.day, d.light, "hrv_baja_1d")
            if d.light == "amber" and d.day <= LUNES - timedelta(days=3)
            else d
            for d in dec
        ]

    def test_un_empate_se_dice_en_vez_de_callarse(self, cfg):
        """Dos temas a tres días cada uno: no hay UNO que mande, hay DOS.

        Antes esto se callaba, y callarlo tenía el efecto contrario al que
        parece. La racha se contaba igual pero sin una palabra sobre qué la
        producía, así que se leía como una racha sin explicación cuando lo que
        había era una con dos. Elegir uno de los dos sería inventarse el motivo;
        nombrar los dos es decir exactamente lo que se sabe.
        """
        t = evaluar_tendencia(cfg, LUNES, self._empate_de_dos())
        assert "racha" in tipos(t)
        assert "Manda el HRV y el sueño a partes iguales" in textos(t)

    def test_el_empate_se_cuenta_siempre_igual(self, cfg):
        """El orden de los temas empatados no puede depender del de llegada.

        Están empatados, así que no hay frecuencia que los ordene y el criterio
        tiene que ser explícito. Si saliera del orden de inserción del
        diccionario, la misma racha se contaría de dos maneras según qué día se
        mirase, y una frase que cambia sin que cambien los datos no vale nada.
        """
        dec = self._empate_de_dos()
        derecho = textos(evaluar_tendencia(cfg, LUNES, dec))
        del_reves = textos(evaluar_tendencia(cfg, LUNES, list(reversed(dec))))
        assert "a partes iguales" in derecho
        assert derecho == del_reves

    def test_con_el_empate_repartido_no_se_enumera_la_lista_entera(self, cfg):
        """Cinco temas a un día cada uno: lo que hay que decir no es cuáles son.

        Enumerarlos convierte la pista en un inventario, y un inventario a las
        siete de la mañana no lo lee nadie. Con el reparto plano la información
        es justo que NINGUNO manda, y eso cabe en una frase.
        """
        reglas = iter([
            "sueno_corto", "hrv_baja_1d", "rhr_alta", "carga_acumulada",
            "lumbar_molesta",
        ])
        dec = [
            DecisionDia(d.day, d.light, next(reglas)) if d.light == "amber" else d
            for d in racha_de(LUNES, 5, "sueno_corto")
        ]
        t = evaluar_tendencia(cfg, LUNES, dec)
        assert "racha" in tipos(t)
        assert "No manda ninguno" in textos(t)
        assert "a partes iguales" not in textos(t)

    def test_el_empate_de_la_racha_no_cambia_el_de_motivo(self, cfg):
        """La misma palabra, dos significados, y solo uno cambia.

        Dentro de una SEMANA un empate es ausencia de señal: no dice que el tema
        haya cambiado, dice que esa semana no opina, y por eso se atraviesa como
        neutra en vez de romper la racha de motivo (`NEUTRAS_SEGUIDAS_MAX`). Si
        al hacer hablar al empate de la racha se hubiera tocado `_dominante`,
        media racha real se partiría en dos por una semana sin opinión.
        """
        from app.engine.tendencia import _dominante, _dominantes

        assert _dominante({"el sueño": 3, "el HRV": 3}) is None
        assert _dominantes({"el sueño": 3, "el HRV": 3}) == ["el HRV", "el sueño"]
        assert _dominante({"el sueño": 4, "el HRV": 3}) == "el sueño"
        assert _dominante({}) is None
        assert _dominantes({}) == []

    def test_avisa_de_que_el_semaforo_no_lo_sabe(self, cfg):
        """La frase que justifica el detector: ninguna regla mira tan atrás."""
        t = evaluar_tendencia(cfg, LUNES, racha_de(LUNES, 5, "hrv_baja_1d"))
        assert "Ninguna regla del semáforo mira tan atrás" in textos(t)

    def test_sin_historico_suficiente_es_na_no_silencio(self, cfg):
        t = evaluar_tendencia(cfg, LUNES, serie(LUNES, 3, "amber", "sueno_corto"))
        assert "racha" in na(t)


# ---------------------------------------------------------------------------
# Detector 3 — motivo
# ---------------------------------------------------------------------------


DOMINGO = date(2026, 5, 31)   # domingo; la semana siguiente empieza el 01/06


class TestMotivo:
    """El detector que dice QUÉ pasa, y el que más cerca estuvo de nacer muerto.

    Con dominancia por nombre de regla y tolerancia cero a las semanas neutras,
    disparaba 0 veces en 179 días de histórico real. Los dos tests que lo
    sujetan ahora son `test_una_semana_neutra_es_transparente` y
    `test_dos_intensidades_del_mismo_tema_no_empatan`.
    """

    def test_cuatro_semanas_del_mismo_tema_disparan(self, cfg):
        dec = semanas(DOMINGO, ["sueno_corto"] * 4)
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=1), dec)
        assert "motivo" in tipos(t)
        assert "el sueño manda por cuarta semana" in textos(t)

    def test_tres_semanas_no_bastan(self, cfg):
        dec = semanas(DOMINGO, ["sueno_corto"] * 3)
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=1), dec)
        assert "motivo" not in tipos(t)

    def test_otro_tema_rompe_la_racha(self, cfg):
        """Seis semanas de sueño con una de HRV metida en medio son cuatro.

        Lo que rompe la racha no es una semana callada, es una semana que dice
        otra cosa. Si el HRV no cortara, el ordinal diría "sexta".
        """
        dec = semanas(DOMINGO, ["sueno_corto", "sueno_corto", "hrv_baja_1d",
                                "sueno_corto", "sueno_corto", "sueno_corto",
                                "sueno_corto"])
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=1), dec)
        assert "motivo" in tipos(t)
        assert "cuarta semana" in textos(t)
        assert "sexta" not in textos(t)

    def test_una_semana_neutra_es_transparente(self, cfg):
        """Una semana que no dice nada no dice que el tema haya cambiado.

        Es la misma disciplina que separa `skipped` de `not_fired` en
        `rules.py`, y sin ella este detector no disparaba nunca: en el histórico
        real había dos tramos de tres semanas partidos, cada uno, por una semana
        tranquila con un empate a uno.
        """
        dec = semanas(DOMINGO, ["sueno_corto", "sueno_corto", None,
                                "sueno_corto", "sueno_corto"])
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=1), dec)
        assert "motivo" in tipos(t)
        assert "cuarta semana" in textos(t)

    def test_la_semana_neutra_no_cuenta_para_el_ordinal_pero_si_se_dice(self, cfg):
        """"Cuarta semana" son cuatro semanas mandando, no cuatro de calendario.

        Y como el tramo abarcado es de cinco, el aviso lo dice: si no, el hueco
        se escondería dentro del número.
        """
        dec = semanas(DOMINGO, ["sueno_corto", "sueno_corto", None,
                                "sueno_corto", "sueno_corto"])
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=1), dec)
        assert "cuarta semana" in textos(t)
        assert "1 semana sin dominante por medio" in textos(t)

    def test_dos_semanas_neutras_seguidas_si_rompen(self, cfg):
        dec = semanas(DOMINGO, ["sueno_corto", "sueno_corto", None, None,
                                "sueno_corto", "sueno_corto"])
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=1), dec)
        assert "motivo" not in tipos(t)

    def test_la_constante_es_uno(self):
        """Si alguien la sube, que sea leyendo por qué no es 0 ni 2."""
        assert NEUTRAS_SEGUIDAS_MAX == 1

    def test_dos_intensidades_del_mismo_tema_no_empatan(self, cfg):
        """`sueno_corto` y `sueno_muy_corto` son el mismo problema.

        Contándolas por separado, una semana con cuatro de una y tres de la otra
        era un empate y la semana salía neutra. Son la misma cosa a dos
        intensidades: cuentan juntas, y por eso la dominancia va por TEMA.
        """
        dec: list[DecisionDia] = []
        for idx in range(4):
            domingo = DOMINGO - timedelta(days=7 * (3 - idx))
            lunes = domingo - timedelta(days=6)
            for d in range(7):
                regla = "sueno_corto" if d < 4 else "sueno_muy_corto"
                dec.append(DecisionDia(lunes + timedelta(days=d), "amber", regla))
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=1), dec)
        assert "motivo" in tipos(t)
        assert "el sueño manda" in textos(t)
        # Y el desglose por regla sigue estando, que es lo que se pierde al
        # agrupar por tema y hay que devolver en el texto.
        assert "sueno_corto" in textos(t) and "sueno_muy_corto" in textos(t)

    def test_la_semana_en_curso_no_cuenta(self, cfg):
        """Un lunes malo no es "una semana mala".

        Si la semana en curso contara, el aviso del jueves contradiría al del
        lunes sin que hubiera pasado nada nuevo. Solo semanas ISO completas.
        """
        dec = semanas(DOMINGO, ["sueno_corto"] * 4)
        # Tres días más de otro tema, ya dentro de la semana en curso.
        for d in range(3):
            dec.append(DecisionDia(DOMINGO + timedelta(days=d + 1), "amber",
                                   "carga_acumulada"))
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=3), dec)
        assert "el sueño manda por cuarta semana" in textos(t)

    def test_semana_a_medias_es_na_no_silencio(self, cfg):
        dec = semanas(DOMINGO, ["sueno_corto"] * 4, dias=2)
        t = evaluar_tendencia(cfg, DOMINGO + timedelta(days=1), dec)
        assert "motivo" in na(t)
        assert "motivo" not in tipos(t)


# ---------------------------------------------------------------------------
# Detector 2 — ventana
# ---------------------------------------------------------------------------


class TestVentana:
    def test_poca_cobertura_es_na(self, cfg):
        t = evaluar_tendencia(cfg, LUNES, serie(LUNES, 20, "amber", "sueno_corto"))
        assert "ventana" in na(t)

    def test_sin_diferencia_no_dispara(self, cfg):
        """90 días iguales: el último mes no ha ido peor que el trimestre."""
        dec = [
            DecisionDia(LUNES - timedelta(days=i),
                        "amber" if i % 2 else "green",
                        "sueno_corto" if i % 2 else None)
            for i in range(120)
        ]
        t = evaluar_tendencia(cfg, LUNES, dec)
        assert "ventana" not in tipos(t)
        assert "ventana" not in na(t)

    def test_un_mes_claramente_peor_dispara(self, cfg):
        dec = serie(LUNES - timedelta(days=30), 90, "green")
        dec += serie(LUNES, 30, "amber", "sueno_corto")
        t = evaluar_tendencia(cfg, LUNES, dec)
        assert "ventana" in tipos(t)

    def test_se_etiqueta_como_retrospectivo(self, cfg):
        """La condición bajo la que se aceptó este detector.

        Llega tarde por construcción y se queda igualmente, pero solo si el
        texto impide leerlo como una alerta temprana. Estas dos frases son esa
        condición: si alguien las quita, el detector deja de ser lo que se
        aprobó.
        """
        dec = serie(LUNES - timedelta(days=30), 90, "green")
        dec += serie(LUNES, 30, "amber", "sueno_corto")
        t = evaluar_tendencia(cfg, LUNES, dec)
        texto = textos(t)
        assert texto.count("RETROSPECTIVO") >= 1
        assert "no avisa de lo que viene" in texto


# ---------------------------------------------------------------------------
# Ventanas disjuntas
# ---------------------------------------------------------------------------
# Una ventana corta metida DENTRO de su propia referencia se compara en parte
# consigo misma. No es un matiz de precisión: amortigua la señal alrededor de un
# tercio y, bajo una deriva sostenida, la referencia persigue a la ventana y el
# hueco no se abre nunca. Es el mismo filtro de paso alto que deja a `hrv_ratio`
# encerrado entre 0,91 y 1,04 en seis meses.
#
# Estos tests son los que distinguen las dos implementaciones. Si alguien
# devuelve el anidamiento, el resto de la suite sigue en verde y estos no.


class TestVentanasDisjuntas:
    def test_la_referencia_no_incluye_los_dias_recientes(self):
        """La pieza de abajo, medida a mano sobre números que no se prestan.

        Treinta días a 10 y sesenta anteriores a 100. La media de los 60 de
        antes es 100; si la ventana larga arrastrara los 30 recientes daría
        70 -la media de los 90-, que es la vieja.

        Este test fija `_media` y NADA MÁS. Que el parámetro exista y calcule
        bien no dice que nadie lo use: borrando los `desde=corta` de los tres
        sitios que lo llaman, este test sigue verde. Los que vigilan las
        llamadas son los tres de abajo, y por eso cada uno lleva escrito el
        número que sale con el anidamiento.
        """
        from app.engine.tendencia import _media

        hoy = LUNES
        serie_ = {hoy - timedelta(days=i): (10.0 if i < 30 else 100.0) for i in range(90)}
        assert _media(serie_, hoy, 30) == (10.0, 30)
        assert _media(serie_, hoy, 90, desde=30) == (100.0, 60)
        assert _media(serie_, hoy, 90)[0] == 70.0, "la de siempre, para contraste"

    def test_una_deriva_sostenida_abre_hueco_en_vez_de_taparse(self, cfg):
        """El fallo que no se ve mirando un caso a caso.

        Empeoramiento monótono y lento: 12 días malos hace tres meses, 16 hace
        dos, 20 el último mes. Con las ventanas separadas el hueco es 66,7%
        contra 46,7% = 20 puntos, y el detector habla. Con la referencia
        anidada, el mes reciente entra en su propia referencia y la sube a
        53,3%: quedan 13,3 puntos y el detector se calla, justo en la deriva
        que existe para ver.

        Los dos números no son independientes: la ventana corta es un tercio
        de la larga, así que el hueco disjunto es SIEMPRE exactamente 1,5
        veces el anidado. Por eso la construcción está elegida para que el
        anidado caiga en [10, 15) y el umbral de 15 puntos separe los dos.
        """
        dec = []
        for i in range(90):
            if i < 30:                        # el último mes: 20 de 30 malos
                malo = i % 3 != 0
            elif i < 60:                      # el anterior: 16 de 30
                malo = (i - 30) % 15 < 8
            else:                             # el de antes: 12 de 30
                malo = (i - 60) % 5 < 2
            d = LUNES - timedelta(days=i)
            dec.append(DecisionDia(d, "amber" if malo else "green",
                                   "sueno_corto" if malo else None))
        t = evaluar_tendencia(cfg, LUNES, dec)
        assert "ventana" in tipos(t), (
            "con la referencia anidada el hueco se queda en 13 puntos, por "
            "debajo del umbral de 15, y el detector se calla justo en la "
            "deriva que existe para ver"
        )
        assert "+20 puntos" in textos(t), (
            f"el hueco de verdad son 20 puntos, no los 13 amortiguados: "
            f"{textos(t)}"
        )

    def test_el_texto_dice_contra_que_se_compara(self, cfg):
        """Si el mensaje dijera «el trimestre» estaría mintiendo sobre la cuenta.

        Y no es cosmética: quien lea «frente al trimestre» y vaya a comprobarlo
        a mano sacará otro número y pensará que el sistema está roto.

        El caso es el extremo a propósito, porque es donde la mentira se ve de
        golpe: un mes entero en ámbar detrás de un trimestre entero en verde.
        Separadas, eso es 100% contra 0%. Anidada, la referencia se come el
        mes malo y dice "frente a 33%", que es un número que no corresponde a
        ningún tramo de la serie -ni a los 90 días, ni a los 60 de antes-, y
        aun así el mensaje lo etiqueta como "los 60 de antes". Por eso el
        nombre del tramo no basta como prueba: hay que exigir también el
        número, porque la frase se construye igual de bien mintiendo.
        """
        dec = serie(LUNES - timedelta(days=30), 90, "green")
        dec += serie(LUNES, 30, "amber", "sueno_corto")
        texto = textos(evaluar_tendencia(cfg, LUNES, dec))
        assert "60 días anteriores" in texto or "60 de antes" in texto
        assert "trimestre" not in texto
        assert "frente a 0%" in texto, (
            f"los 60 días anteriores están todos en verde; un 33% ahí es la "
            f"referencia anidada contándose el mes ámbar a sí misma: {texto}"
        )
        assert "+100 puntos" in texto, f"anidada imprime +67: {texto}"

    def test_el_cualificador_de_sueno_usa_la_misma_separacion(self, cfg):
        """Mismo arreglo en el otro sitio donde estaba el mismo fallo.

        Los 30 recientes a 380 min y los 60 anteriores a 420: la caída de
        verdad son 40 minutos. Anidada, la referencia sale en 406,7 y la caída
        se queda en 26,7 — una rebaja de un tercio que acerca peligrosamente
        cualquier umbral a no dispararse.
        """
        ssc, smin = series_sueno(LUNES, 120, 380, 420, 80, 80)
        dec = racha_de(LUNES, 6, "sueno_corto", colchon=120)
        texto = textos(evaluar_tendencia(cfg, LUNES, dec,
                                         sleep_score=ssc, sleep_min=smin))
        assert "40 min menos" in texto, (
            f"la caída tiene que ser la de verdad, no la amortiguada: {texto}"
        )
        assert "60 días anteriores" in texto


# ---------------------------------------------------------------------------
# Cualificador de sueño
# ---------------------------------------------------------------------------


def series_sueno(hasta: date, dias: int, corta_min: float, larga_min: float,
                 corta_score: float, larga_score: float):
    """Dos series planas: los últimos 30 días a un valor, el resto a otro."""
    smin: dict[date, float] = {}
    ssc: dict[date, float] = {}
    for i in range(dias):
        d = hasta - timedelta(days=i)
        smin[d] = corta_min if i < 30 else larga_min
        ssc[d] = corta_score if i < 30 else larga_score
    return ssc, smin


class TestCualificadorSueno:
    """Separa cantidad de calidad, que son dos problemas con dos arreglos.

    Vive aquí y no en una regla del semáforo por decisión explícita: la regla
    del cuadrante de disociación disparaba 4 veces en seis meses sin añadir un
    solo día distinto, que es la definición de opción muerta.
    """

    def _dec(self):
        return racha_de(LUNES, 6, "sueno_corto", colchon=120)

    def test_solo_cantidad(self, cfg):
        ssc, smin = series_sueno(LUNES, 120, 380, 420, 80, 80)
        t = evaluar_tendencia(cfg, LUNES, self._dec(),
                              sleep_score=ssc, sleep_min=smin)
        assert "es cantidad" in textos(t)
        assert "calidad igual" in textos(t)

    def test_solo_calidad(self, cfg):
        ssc, smin = series_sueno(LUNES, 120, 420, 420, 70, 80)
        t = evaluar_tendencia(cfg, LUNES, self._dec(),
                              sleep_score=ssc, sleep_min=smin)
        assert "calidad" in textos(t)
        assert "es cantidad" not in textos(t)

    def test_las_dos_cosas(self, cfg):
        ssc, smin = series_sueno(LUNES, 120, 380, 420, 70, 80)
        t = evaluar_tendencia(cfg, LUNES, self._dec(),
                              sleep_score=ssc, sleep_min=smin)
        assert "las dos cosas" in textos(t)

    def test_ninguna_de_las_dos_tambien_es_respuesta(self, cfg):
        """El veredicto que más sale en el histórico real, y no es un hueco.

        Dice algo concreto: el semáforo señala al sueño porque un puñado de
        noches cruzaron el umbral, no porque el mes haya sido peor que el
        trimestre. Se mueve el umbral, no el descanso.
        """
        ssc, smin = series_sueno(LUNES, 120, 420, 420, 80, 80)
        t = evaluar_tendencia(cfg, LUNES, self._dec(),
                              sleep_score=ssc, sleep_min=smin)
        assert "no ha empeorado" in textos(t)
        assert "sueño" not in na(t)

    def test_sin_series_es_na_con_motivo(self, cfg):
        t = evaluar_tendencia(cfg, LUNES, self._dec())
        assert "sueño" in na(t)

    def test_solo_minutos_lo_dice(self, cfg):
        _, smin = series_sueno(LUNES, 120, 380, 420, 80, 80)
        t = evaluar_tendencia(cfg, LUNES, self._dec(), sleep_min=smin)
        motivos = " ".join(s.motivo for s in t.sin_muestra)
        assert "no `sleep_score` suficiente" in motivos

    def test_solo_score_lo_dice(self, cfg):
        ssc, _ = series_sueno(LUNES, 120, 380, 420, 80, 80)
        t = evaluar_tendencia(cfg, LUNES, self._dec(), sleep_score=ssc)
        motivos = " ".join(s.motivo for s in t.sin_muestra)
        assert "no minutos suficientes" in motivos

    def test_no_se_engancha_a_un_aviso_que_no_es_de_sueno(self, cfg):
        """Si manda el HRV, el matiz del sueño sobra y además despista."""
        ssc, smin = series_sueno(LUNES, 120, 380, 420, 70, 80)
        dec = racha_de(LUNES, 6, "hrv_baja_1d", colchon=120)
        t = evaluar_tendencia(cfg, LUNES, dec, sleep_score=ssc, sleep_min=smin)
        assert "cantidad" not in textos(t)
        assert "sueño" not in na(t)

    def test_los_umbrales_son_alcanzables(self, cfg):
        """Un umbral que el histórico no alcanza nunca es folklore.

        La primera versión pedía 5 puntos de caída de `sleep_score` cuando el
        máximo observado en 179 días fue 2,6: dos de los cuatro veredictos eran
        opciones muertas recién nacidas. Este test no vuelve a medir el
        histórico -eso lo hace `replay_semaforo.py --tendencia`- pero sí impide
        que alguien los devuelva a un rango imposible sin enterarse.
        """
        sueno = cfg.raw["trend"]["sueno"]
        assert sueno["caida_score_min"] <= 3
        assert sueno["caida_min_min"] <= 26


# ---------------------------------------------------------------------------
# De regla a tema
# ---------------------------------------------------------------------------


class TestTemas:
    def test_las_dos_de_sueno_comparten_tema(self, cfg):
        assert (tema_de_regla(cfg.raw, "sueno_corto")
                == tema_de_regla(cfg.raw, "sueno_muy_corto")
                == "el sueño")

    def test_las_dos_de_hrv_comparten_tema(self, cfg):
        assert (tema_de_regla(cfg.raw, "hrv_baja_1d")
                == tema_de_regla(cfg.raw, "hrv_hundida_2d")
                == "el HRV")

    def test_una_regla_mixta_no_elige_tema(self, cfg):
        """`sin_ganas_y_reventado` mira cansancio Y ganas: resumirla es perderla."""
        assert tema_de_regla(cfg.raw, "sin_ganas_y_reventado") == "«sin_ganas_y_reventado»"

    def test_regla_inexistente_no_revienta(self, cfg):
        """Una regla borrada del YAML puede seguir viva en el histórico."""
        assert tema_de_regla(cfg.raw, "regla_que_ya_no_existe") == "«regla_que_ya_no_existe»"

    def test_todas_las_reglas_reales_tienen_tema(self, cfg):
        """Si alguien añade una regla con una señal nueva, que se entere aquí.

        Una regla sin tema cae en el `«nombre»` de emergencia, que funciona pero
        convierte el aviso de motivo en jerga interna: "«resaca_finde» manda por
        quinta semana" no lo lee nadie a las 7 de la mañana.
        """
        mixtas = {"sin_ganas_y_reventado"}
        for nivel in ("red", "amber"):
            for regla in cfg.raw["thresholds"].get(nivel) or []:
                nombre = regla["name"]
                if nombre in mixtas:
                    continue
                assert not tema_de_regla(cfg.raw, nombre).startswith("«"), nombre

    def test_las_senales_salen_del_arbol_no_de_requires(self, cfg):
        """`requires` es documentación y puede mentir; `when` es lo que se evalúa."""
        regla = next(r for r in cfg.raw["thresholds"]["amber"]
                     if r["name"] == "resaca_finde")
        assert senales_de_regla(regla) == {"weekend_intense_rides",
                                           "weekend_total_hours"}


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------


class TestSalida:
    def test_sin_novedad_cuando_no_hay_nada_que_decir(self, cfg):
        """Distinto de "sin muestra": aquí sí se ha mirado y no había nada.

        Cuatro meses de un día malo de cada tres, con el tema rotando cada
        semana: no hay racha de cinco, no hay cuatro semanas del mismo tema y el
        mes va igual que el trimestre. Los tres detectores miran y no ven nada,
        que es un resultado y no un hueco.
        """
        rotacion = ["sueno_corto", "hrv_baja_1d", "carga_acumulada"]
        dec = []
        for i in range(120):
            d = LUNES - timedelta(days=i)
            if i % 3 == 0:
                dec.append(DecisionDia(d, "amber", rotacion[d.isocalendar()[1] % 3]))
            else:
                dec.append(DecisionDia(d, "green", None))
        t = evaluar_tendencia(cfg, LUNES, dec)
        assert t.lineas() == ["Tendencia: sin novedad"]

    def test_cada_linea_lleva_el_prefijo(self, cfg):
        t = evaluar_tendencia(cfg, LUNES, racha_de(LUNES, 6, "sueno_corto"))
        assert all(l.startswith("Tendencia: ") for l in t.lineas())

    def test_los_na_se_juntan_en_una_linea(self, cfg):
        """Tres N/A en tres líneas serían tres cuartas partes del mensaje."""
        t = evaluar_tendencia(cfg, LUNES, serie(LUNES, 3, "amber", "sueno_corto"))
        lineas = [l for l in t.lineas() if "sin muestra" in l]
        assert len(lineas) == 1
        assert lineas[0].count("·") == 2

    def test_el_orden_es_motivo_racha_ventana(self, cfg):
        """De lo que más dice a lo que menos: el porqué, el cuánto, el retrovisor."""
        dec = serie(LUNES - timedelta(days=30), 90, "green")
        dec += serie(LUNES, 30, "amber", "sueno_corto")
        t = evaluar_tendencia(cfg, LUNES, dec)
        orden = [a.tipo for a in t.avisos]
        assert orden == sorted(orden, key=["motivo", "racha", "ventana"].index)

    def test_to_dict_es_serializable(self, cfg):
        import json
        t = evaluar_tendencia(cfg, LUNES, racha_de(LUNES, 6, "sueno_corto"))
        d = t.to_dict()
        assert json.loads(json.dumps(d))["day"] == LUNES.isoformat()
        assert d["lineas"] == t.lineas()
        assert d["activa"] is True


# ---------------------------------------------------------------------------
# La capa es pura
# ---------------------------------------------------------------------------


class TestPureza:
    def test_no_toca_la_lista_que_recibe(self, cfg):
        """El replay la llama 179 veces sobre la misma lista que va creciendo."""
        dec = racha_de(LUNES, 6, "sueno_corto")
        copia = copy.deepcopy(dec)
        evaluar_tendencia(cfg, LUNES, dec)
        assert dec == copia

    def test_mismo_histórico_misma_salida(self, cfg):
        dec = racha_de(LUNES, 6, "sueno_corto")
        a = evaluar_tendencia(cfg, LUNES, dec).to_dict()
        b = evaluar_tendencia(cfg, LUNES, list(reversed(dec))).to_dict()
        assert a == b

    def test_no_lee_la_base_de_datos(self, cfg):
        """Recibe `DecisionDia`, no una sesión.

        Es lo que permite al replay alimentarla con decisiones que nunca
        existieron -las que el motor HABRÍA tomado- y sin eso los cinco umbrales
        absolutos no se podrían falsar contra nada.
        """
        import inspect

        import app.engine.tendencia as modulo
        fuente = inspect.getsource(modulo)
        assert "session" not in fuente.lower().replace("decisiones", "")
