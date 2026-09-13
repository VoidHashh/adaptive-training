"""Los tres botones de comprobación: que digan la verdad, incluso la incómoda.

QUÉ SE PRUEBA AQUÍ Y QUÉ NO
---------------------------
No se prueba que Garmin responda ni que Hevy exista: eso se comprueba contra
los servicios de verdad, y se ha hecho. Lo que se prueba aquí es lo único que
puede estar mal sin que nadie lo note: **el informe**.

Un botón de diagnóstico que se pinta verde cuando algo falla es peor que no
tener botón, porque sustituye una duda por una certeza falsa. Así que la
batería va casi entera contra esa forma de fallar: cadenas cortadas que se
pintan enteras, pasos que no se hicieron contados como buenos, y avisos que se
calculan antes de que exista lo que tienen que avisar.
"""

from __future__ import annotations

from datetime import date, timedelta

from app import diagnostico as dg
from app.diagnostico import DIAS_ATRAS_LECTURA, Paso, Resultado


# ---------------------------------------------------------------------------
# El informe
# ---------------------------------------------------------------------------


def test_un_paso_que_no_se_pudo_hacer_no_cuenta_como_bueno():
    """`ok=None` es "no se intentó", y no se intentó no es "salió bien".

    Si contara como bueno, una cadena que se corta en el primer eslabón se
    pintaría verde entera: la manera más rápida de que un botón de diagnóstico
    mienta.

    OJO: este test NO sujeta él solo la diferencia entre `is True` y
    `is not False`, porque el paso en rojo ya basta para tumbar el informe y el
    `None` viaja de gorra. Eso lo sujeta el test de abajo. Se deja escrito
    porque es el caso real -primero revienta algo, luego lo que venía detrás no
    se intenta- y porque el que pasa por el motivo equivocado es el que nadie
    revisa después.
    """
    r = Resultado("X")
    r.paso("uno", False, "reventó")
    r.paso("dos", None, "no se intentó")
    assert r.ok is False


def test_un_paso_sin_intentar_entre_pasos_buenos_basta_para_no_pintar_verde():
    """Aquí el `None` es lo ÚNICO que decide, y por eso este test sí sirve.

    Sin ningún paso en rojo que lo tape, cambiar `p.ok is True` por
    `p.ok is not False` pintaría este informe de verde: un diagnóstico que dice
    "todo correcto" habiendo dejado un eslabón sin comprobar.
    """
    r = Resultado("X")
    r.paso("uno", True, "bien")
    r.paso("dos", None, "no se intentó")
    assert r.ok is False


def test_una_lista_de_pasos_vacia_no_es_todo_correcto():
    """Sin pasos no hay comprobación, y sin comprobación no hay verde."""
    assert Resultado("X").ok is False


def test_el_resumen_nombra_el_fallo_en_vez_de_contar_cuantos_salieron_bien():
    """"3 de 5 pasos bien" obliga a abrir la ficha para saber qué pasa.

    Lo único que se lee de un vistazo tiene que ser la causa.
    """
    r = Resultado("Garmin")
    r.paso("credenciales", True, "configuradas")
    r.paso("sesión", False, "no se pudo entrar")
    d = r.como_dict()
    assert d["resumen"] == "Garmin: no se pudo entrar"
    assert "3 de" not in d["resumen"]


def test_el_resumen_enseña_el_primer_fallo_y_no_el_ultimo():
    """El primero es la causa; los de después suelen ser consecuencias."""
    r = Resultado("Garmin")
    r.paso("sesión", False, "no se pudo entrar")
    r.paso("lectura", False, "no vino nada")
    assert r.como_dict()["resumen"] == "Garmin: no se pudo entrar"


def test_todos_los_pasos_bien_es_lo_unico_que_pinta_verde():
    r = Resultado("Hevy")
    r.paso("credenciales", True, "ok")
    r.paso("lectura", True, "ok")
    assert r.ok is True
    assert r.como_dict()["resumen"] == "Hevy: todo correcto"


def test_el_paso_viaja_entero_al_cliente():
    """Si el error no cruza, el aviso obliga a entrar por SSH a buscarlo."""
    d = Paso("sesión", False, "no se pudo entrar", "429").como_dict()
    assert d == {
        "nombre": "sesión",
        "ok": False,
        "detalle": "no se pudo entrar",
        "error": "429",
    }


# ---------------------------------------------------------------------------
# Garmin
# ---------------------------------------------------------------------------


class _ClienteFalso:
    """Un cliente de Garmin de mentira, con los estados que importan."""

    def __init__(self, **kw):
        self.session_resumed = kw.get("session_resumed", True)
        self.tokens_guardados = kw.get("tokens_guardados", None)
        self.rate_limit_events = list(kw.get("rate_limit_events", []))
        self.token_dir = "/app/data/garmin_tokens"
        self._al_leer = kw.get("al_leer")
        self._metricas = kw.get("metricas")

    def connect(self):
        return None

    def day_metrics(self, dia):
        if self._al_leer is not None:
            self._al_leer(self)
        return self._metricas


class _Metricas:
    def __init__(self, **kw):
        self.hrv = kw.get("hrv")
        self.rhr = kw.get("rhr")
        self.sleep_min = kw.get("sleep_min")
        self.sleep_score = kw.get("sleep_score")
        self.body_battery = kw.get("body_battery")


def _con_cliente(monkeypatch, cliente):
    import app.integrations.garmin as g

    monkeypatch.setattr(g, "build_client", lambda *a, **k: cliente)


def _paso(d, nombre):
    return next((p for p in d["pasos"] if p["nombre"] == nombre), None)


def _completas():
    return _Metricas(hrv=60, rhr=52, sleep_min=420, sleep_score=80, body_battery=70)


def test_no_saber_si_hubo_login_no_se_dobla_a_login_nuevo(monkeypatch):
    """Tres estados, tres frases.

    "Hubo login" es una advertencia; "no he podido averiguarlo" es una pregunta
    abierta sobre la librería. Juntarlas pierde justo la información que haría
    falta para saber si hay que ir a mirar el cliente.
    """
    _con_cliente(
        monkeypatch,
        _ClienteFalso(session_resumed=None, metricas=_completas()),
    )
    d = dg.probar_garmin(object(), None)
    detalle = _paso(d, "sesión")["detalle"]
    assert "no se ha podido saber" in detalle
    assert "login nuevo" not in detalle


def test_una_sesion_reanudada_dice_que_repetir_sale_gratis(monkeypatch):
    _con_cliente(
        monkeypatch,
        _ClienteFalso(session_resumed=True, metricas=_completas()),
    )
    d = dg.probar_garmin(object(), None)
    assert "reanudada" in _paso(d, "sesión")["detalle"]


def test_un_login_nuevo_avisa_de_no_insistir(monkeypatch):
    """Es el aviso que evita que la comprobación se convierta en la avería."""
    _con_cliente(
        monkeypatch,
        _ClienteFalso(session_resumed=False, metricas=_completas()),
    )
    d = dg.probar_garmin(object(), None)
    assert "no repetir" in _paso(d, "sesión")["detalle"]


def test_un_login_que_no_guardo_la_sesion_pinta_rojo(monkeypatch):
    """Leer bien hoy y no guardar nada no es "todo correcto".

    Es el fallo que se repite cada mañana sin dar la cara, y el botón existe
    sobre todo para esto.
    """
    _con_cliente(
        monkeypatch,
        _ClienteFalso(
            session_resumed=False, tokens_guardados=False, metricas=_completas()
        ),
    )
    d = dg.probar_garmin(object(), None)
    assert d["ok"] is False
    assert _paso(d, "tokens")["ok"] is False
    assert "repetirá el login" in _paso(d, "tokens")["detalle"]


def test_sin_login_no_se_afirma_nada_sobre_los_tokens(monkeypatch):
    """Si la sesión se reanudó no había nada que guardar.

    Un paso "tokens: bien" aquí sería una comprobación que no se ha hecho.
    """
    _con_cliente(
        monkeypatch,
        _ClienteFalso(
            session_resumed=True, tokens_guardados=None, metricas=_completas()
        ),
    )
    d = dg.probar_garmin(object(), None)
    assert _paso(d, "tokens") is None


def test_los_429_de_la_lectura_tambien_salen_en_el_informe(monkeypatch):
    """EL PASO VA AL FINAL, Y ESTO LO SUJETA.

    Los 429 no solo salen del login: cada lectura de wellness puede topar con
    el límite y reintentar por dentro. Montado justo después de conectar, este
    paso contaría los del login y se perdería los de la lectura, que son los
    que convierten una respuesta aparentemente limpia en una que ha costado
    cinco intentos.
    """
    cliente = _ClienteFalso(
        session_resumed=True,
        metricas=_completas(),
        al_leer=lambda c: c.rate_limit_events.append("429 en sleep"),
    )
    _con_cliente(monkeypatch, cliente)
    d = dg.probar_garmin(object(), None)

    limite = _paso(d, "límite de Garmin")
    assert limite is not None, (
        "el 429 ocurrió durante la lectura y no aparece: el paso se está "
        "calculando antes de que exista lo que tiene que contar"
    )
    assert limite["ok"] is False
    assert d["ok"] is False


def test_una_lectura_fallida_sigue_contando_sus_429(monkeypatch):
    """Cuando la lectura falla es cuando más importa saber si fue el límite."""

    def revienta(c):
        c.rate_limit_events.append("429 en hrv")
        raise RuntimeError("429 Too Many Requests")

    _con_cliente(
        monkeypatch, _ClienteFalso(session_resumed=True, al_leer=revienta)
    )
    d = dg.probar_garmin(object(), None)
    assert _paso(d, "lectura")["ok"] is False
    assert _paso(d, "límite de Garmin") is not None


def test_sin_429_no_se_inventa_el_paso(monkeypatch):
    """Un informe limpio no lleva una fila que diga "0 limitaciones"."""
    _con_cliente(
        monkeypatch, _ClienteFalso(session_resumed=True, metricas=_completas())
    )
    d = dg.probar_garmin(object(), None)
    assert _paso(d, "límite de Garmin") is None
    assert d["ok"] is True


def test_conectar_y_no_traer_nada_es_un_fallo_y_no_un_exito(monkeypatch):
    """La sesión vale y los datos no llegan: justo lo que el botón busca."""
    _con_cliente(
        monkeypatch,
        _ClienteFalso(session_resumed=True, metricas=_Metricas()),
    )
    d = dg.probar_garmin(object(), None)
    assert d["ok"] is False
    assert "no trajo ningún dato" in _paso(d, "lectura")["detalle"]


def test_un_hueco_suelto_no_tumba_la_lectura(monkeypatch):
    """Que falte el Body Battery de una noche es un dato, no una avería."""
    _con_cliente(
        monkeypatch,
        _ClienteFalso(
            session_resumed=True,
            metricas=_Metricas(hrv=60, rhr=52, sleep_min=420, sleep_score=80),
        ),
    )
    d = dg.probar_garmin(object(), None)
    assert d["ok"] is True
    assert "sin Body Battery" in _paso(d, "lectura")["detalle"]


def test_se_lee_un_dia_pasado_y_no_el_de_hoy(monkeypatch):
    """El wellness de hoy puede estar a medias porque el reloj no ha sincronizado.

    Un hueco ahí se leería como "Garmin no responde" cuando lo que pasa es que
    son las ocho de la mañana.
    """
    pedidos = []
    cliente = _ClienteFalso(session_resumed=True, metricas=_completas())
    cliente.day_metrics = lambda dia: (pedidos.append(dia), _completas())[1]
    _con_cliente(monkeypatch, cliente)
    dg.probar_garmin(object(), None)

    assert pedidos == [date.today() - timedelta(days=DIAS_ATRAS_LECTURA)]
    assert pedidos[0] != date.today()


def test_si_no_hay_cliente_los_pasos_siguientes_dicen_que_no_se_intentaron(
    monkeypatch,
):
    import app.integrations.garmin as g

    def revienta(*a, **k):
        raise RuntimeError("faltan credenciales")

    monkeypatch.setattr(g, "build_client", revienta)
    d = dg.probar_garmin(object(), None)

    assert d["ok"] is False
    assert [p["ok"] for p in d["pasos"]] == [False, None, None]


# ---------------------------------------------------------------------------
# Hevy
# ---------------------------------------------------------------------------


class _HevyFalso:
    def __init__(self, rutinas, workouts=None):
        self._rutinas = rutinas
        self._workouts = workouts if workouts is not None else [1, 2, 3]
        self.escrituras = []

    def get_workouts(self, since=None):
        return self._workouts

    def get_routine(self, rid):
        if rid not in self._rutinas:
            raise RuntimeError("404 no existe")
        return {"title": self._rutinas[rid]}

    # Si el diagnóstico llamara a cualquiera de estas, el test lo caza.
    def update_routine(self, *a, **k):
        self.escrituras.append("update")
        raise AssertionError("el diagnóstico ha escrito en Hevy")

    def create_routine(self, *a, **k):
        self.escrituras.append("create")
        raise AssertionError("el diagnóstico ha escrito en Hevy")


class _CfgFalso:
    def __init__(self, rutinas):
        self.routines = rutinas


def _con_hevy(monkeypatch, cliente):
    import app.integrations.hevy as h

    monkeypatch.setattr(h, "build_client", lambda *a, **k: cliente)


def test_el_diagnostico_de_hevy_no_escribe_nada(monkeypatch):
    """Una "prueba de escritura" dejaría la rutina del lunes como nadie pidió.

    El día que fallara a la mitad, el estropicio sería obra del botón que
    estaba ahí para detectar estropicios.
    """
    cliente = _HevyFalso({"r1": "Día 1"})
    _con_hevy(monkeypatch, cliente)
    dg.probar_hevy(object(), _CfgFalso({"dia_1": {"hevy_routine_id": "r1"}}))
    assert cliente.escrituras == []


def test_un_titulo_que_no_coincide_no_es_un_fallo(monkeypatch):
    """LOS TÍTULOS NO SON DATO.

    Está medido en este proyecto que 4 de 14 entrenamientos tienen un título
    que miente sobre su propia rutina. Y contra la cuenta real, Hevy devuelve
    "Dia 1 HIIT" donde el YAML dice "Día 1 HIIT": validar el título habría dado
    un fallo inventado por una tilde. Lo que cuenta es el identificador.
    """
    _con_hevy(monkeypatch, _HevyFalso({"r1": "Otra cosa completamente"}))
    d = dg.probar_hevy(
        object(), _CfgFalso({"dia_1": {"hevy_routine_id": "r1"}})
    )
    assert d["ok"] is True
    informativo = _paso(d, "títulos (solo informativos)")
    assert informativo is not None
    assert "solo informativos" in informativo["nombre"], (
        "el título tiene que ir etiquetado como lo que es, o el día que no "
        "coincida alguien lo leerá como una avería"
    )


def test_una_rutina_que_ya_no_existe_se_caza_antes_del_lunes(monkeypatch):
    """Es lo que de verdad aporta esta comprobación.

    Un identificador que apunta a una rutina borrada desde el móvil no da la
    cara hasta la mañana en que toca escribirla, y entonces el día se queda sin
    su versión.
    """
    _con_hevy(monkeypatch, _HevyFalso({"r1": "Día 1"}))
    d = dg.probar_hevy(
        object(),
        _CfgFalso(
            {
                "dia_1": {"hevy_routine_id": "r1"},
                "dia_2": {"hevy_routine_id": "borrada"},
            }
        ),
    )
    assert d["ok"] is False
    assert _paso(d, "rutinas")["ok"] is False
    assert "borrada" in _paso(d, "rutinas")["error"]


def test_cero_entrenamientos_no_es_un_fallo_de_clave(monkeypatch):
    """La lista vacía es una respuesta, y hay que decirlo para que no asuste."""
    _con_hevy(monkeypatch, _HevyFalso({"r1": "Día 1"}, workouts=[]))
    d = dg.probar_hevy(
        object(), _CfgFalso({"dia_1": {"hevy_routine_id": "r1"}})
    )
    assert d["ok"] is True
    assert "la clave funciona" in _paso(d, "lectura")["detalle"]


def test_un_config_sin_rutinas_declaradas_es_un_fallo(monkeypatch):
    """Si no hay identificadores, el lunes no hay nada que reescribir."""
    _con_hevy(monkeypatch, _HevyFalso({}))
    d = dg.probar_hevy(object(), _CfgFalso({}))
    assert d["ok"] is False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def test_telegram_manda_un_mensaje_de_verdad(monkeypatch):
    """Aquí no hay modo seco que valga: el mensaje ES la prueba.

    `dry_run` comprueba que el texto se puede montar, que es lo que ya comprueba
    el runner todas las mañanas. Lo que este botón tiene que demostrar es que
    LLEGA.
    """
    enviados = []

    class Cli:
        def send(self, texto):
            enviados.append(texto)
            return type("R", (), {"ok": True, "partes": 1})()

    import app.integrations.telegram as t

    monkeypatch.setattr(t, "build_client", lambda *a, **k: Cli())
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is True
    assert enviados == [dg.TEXTO_POR_DEFECTO]


def test_el_texto_propio_sustituye_al_de_por_defecto(monkeypatch):
    enviados = []

    class Cli:
        def send(self, texto):
            enviados.append(texto)
            return type("R", (), {"ok": True, "partes": 1})()

    import app.integrations.telegram as t

    monkeypatch.setattr(t, "build_client", lambda *a, **k: Cli())
    dg.probar_telegram(object(), None, texto="hola")
    assert enviados == ["hola"]


def test_un_cliente_que_dice_que_no_envio_no_se_pinta_verde(monkeypatch):
    """`ok=False` en la respuesta es un no, aunque no haya excepción."""

    class Cli:
        def send(self, texto):
            return type("R", (), {"ok": False, "partes": 0})()

    import app.integrations.telegram as t

    monkeypatch.setattr(t, "build_client", lambda *a, **k: Cli())
    d = dg.probar_telegram(object(), None)
    assert d["ok"] is False


def test_sin_cliente_de_telegram_el_envio_no_se_intenta(monkeypatch):
    import app.integrations.telegram as t

    def revienta(*a, **k):
        raise RuntimeError("falta el token")

    monkeypatch.setattr(t, "build_client", revienta)
    d = dg.probar_telegram(object(), None)

    assert d["ok"] is False
    assert _paso(d, "envío")["ok"] is None
    assert "no se intentó" in _paso(d, "envío")["detalle"]
