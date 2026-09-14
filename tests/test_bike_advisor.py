"""El punto de partida de la bici y el techo del semáforo.

Dos cosas, y las dos son la misma frase dicha al revés:

  - el PUNTO DE PARTIDA ya no sale del día de la semana, sino de comparar los
    días transcurridos desde la última salida intensa contra los percentiles de
    los huecos entre intensas del propio histórico;
  - el TECHO DEL SEMÁFORO sigue siendo el único recorte, y sale de lo que mide
    el cuerpo.

El fichero entero está escrito contra el fallo que tuvo esto antes de existir:
el calendario. `recommend_on: [saturday, sunday]` dejaba al sistema mudo de
lunes a viernes -30 de 80 salidas reales sin una palabra, 6 de ellas intensas- y
`baseline_by_weekday` daba por hecho que el sábado toca apretar. Por eso hay un
test que recorre los SIETE días de la semana y exige que salga lo mismo: es la
única forma de que el calendario no vuelva por la puerta de atrás.

Hasta hace poco todo el módulo tenía un solo test, y era `d.bike is not None`.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config_loader import load_config
from app.engine.bike_advisor import BikeConfigError, _cap, recommend_bike
from app.engine.signals import ClassifiedRide, Ride, Signals, percentile

from tests.conftest import LUNES, REPO_ROOT

ORDEN = ["descanso", "suave", "media", "intensa"]
SABADO = LUNES + timedelta(days=5)
DOMINGO = LUNES + timedelta(days=6)
MIERCOLES = LUNES + timedelta(days=2)

# Huecos elegidos para que los percentiles salgan REDONDOS y las fronteras se
# puedan comprobar a mano: con [2, 5, 10, 20, 30] el p40 del proyecto da 8 y el
# p60 da 14 (interpolación lineal, mismo método que numpy). Así las tres bandas
# son exactamente:
#     días < 8        -> suave
#     8 <= días < 14  -> intensa
#     días >= 14      -> media
# Son cinco huecos, uno por encima de `min_gaps: 4`, o sea que cualquier test
# que quiera quedarse sin base solo tiene que recortar la lista.
#
# LOS PERCENTILES SE LEEN DEL CONFIG, NO SE ESCRIBEN AQUÍ
# --------------------------------------------------------
# Antes esto era `[4, 6, 8, 10, 14]` con `P_BAJO, P_ALTO = 6, 10`, calculados a
# mano para el p25/p75 que había entonces. Cuando el config pasó a p40/p60 los
# tres números de arriba dejaron de describir nada y estos tests habrían
# seguido en verde probando unas fronteras que ya no existían, porque un test
# que se inventa sus propias constantes no comprueba el sistema: se comprueba a
# sí mismo. Ahora las fronteras se derivan del YAML real y de estos huecos, así
# que el día que alguien vuelva a mover los percentiles esto se entera solo o
# revienta diciendo por qué.
HUECOS = [2, 5, 10, 20, 30]


SPEC = load_config(REPO_ROOT / "config.yaml").raw["cycling"]["recommendation"][
    "baseline_from_gaps"
]
VENTANA = int(SPEC["window_days"])


def _fronteras() -> tuple[int, int]:
    """Las dos fronteras en días, con el percentil y el YAML de producción.

    Se calcula al importar el módulo, y no dentro de un test, porque hace falta
    en los `parametrize`, que se evalúan al recolectar y no ven las fixtures.
    """
    lo = percentile(HUECOS, float(SPEC["percentile_low"]))
    hi = percentile(HUECOS, float(SPEC["percentile_high"]))
    assert lo == int(lo) and hi == int(hi), (
        f"los huecos {HUECOS} ya no dan fronteras enteras con "
        f"p{SPEC['percentile_low']:g}/p{SPEC['percentile_high']:g}: salen {lo} y "
        f"{hi}. Elige otros huecos, porque con fronteras fraccionarias los casos "
        f"de este fichero dejan de poder comprobarse a mano"
    )
    assert lo < hi, f"las dos fronteras se han juntado en {lo}: no hay banda media"
    return int(lo), int(hi)


P_BAJO, P_ALTO = _fronteras()

# Un día cualquiera de cada banda, para los tests que necesitan "un día normal"
# y no están mirando las fronteras. Antes esto era un `7` escrito a mano en
# veinte sitios, que es justo lo que se rompió al mover los percentiles.
DIAS_SUAVE = P_BAJO - 1
DIAS_INTENSA = P_BAJO + 1
DIAS_MEDIA = P_ALTO + 1

# EL PARÓN MÁS LARGO QUE ESTE HISTORIAL AGUANTA SIN QUEDARSE SIN BASE.
# `_fechas_intensas` construye hacia atrás, así que la salida más antigua cae a
# `dias_desde + sum(HUECOS)` días. En cuanto eso pasa de la ventana, la primera
# intensa se sale, queda un hueco menos y se baja de `min_gaps`: el sistema deja
# de tener base y devuelve `applies=False`. Es correcto -es la guarda haciendo
# su trabajo- pero convierte un test de "un parón largo no pide series" en un
# test de "no hay base", que es otra cosa. Se calcula en vez de elegir un número
# a ojo porque con los huecos anteriores ([4, 6, 8, 10, 14], que sumaban 42) el
# tope era 138 y ahora es 113: un `120` escrito a mano pasaba antes y falla
# ahora, y el motivo no se parece en nada a lo que el test dice mirar.
DIAS_PARON_MAX = VENTANA - sum(HUECOS)


def _fechas_intensas(day: date, dias_desde: int, huecos=HUECOS) -> list[date]:
    """Fechas de salidas intensas que dejan `dias_desde` días desde la última.

    Se construye hacia atrás desde la última para que el número que importa
    -los días transcurridos- sea exacto y no dependa de cuántos huecos haya.
    """
    fechas = [day - timedelta(days=dias_desde)]
    for h in reversed(huecos):
        fechas.append(fechas[-1] - timedelta(days=h))
    return sorted(fechas)


def _senales(
    day: date,
    dias_desde: int | None = DIAS_INTENSA,
    huecos=HUECOS,
    nivel: str = "intensa",
) -> Signals:
    """Señales con un histórico de bici que fija el punto de partida.

    `dias_desde=None` deja el histórico vacío: es el día sin base.
    """
    s = Signals(day=day)
    if dias_desde is None:
        return s
    s.rides = [
        ClassifiedRide(
            ride=Ride(date=d, duration_s=7200),
            level=nivel,
            source="test",
            load=100.0,
            load_estimated=False,
        )
        for d in _fechas_intensas(day, dias_desde, huecos)
    ]
    return s


# --- la guarda de `_cap` ---------------------------------------------------


def test_cap_recorta_al_techo():
    assert _cap("intensa", "suave", ORDEN) == "suave"


def test_cap_no_sube_un_nivel_que_ya_esta_por_debajo():
    """El techo es un máximo, no un objetivo."""
    assert _cap("suave", "intensa", ORDEN) == "suave"


@pytest.mark.parametrize(
    "nivel,techo",
    [("intensa", "moderada"), ("brutal", "suave")],
)
def test_un_nivel_desconocido_revienta_en_vez_de_no_recortar(nivel, techo):
    """Devolver el nivel sin tocar era quitarle el techo al semáforo callando.

    Una función cuyo trabajo es poner un techo no puede tener una rama que
    consiste en no ponerlo: el modo de fallo tiene que ser 'para', no 'sigue'.
    """
    with pytest.raises(BikeConfigError, match="intensity_order"):
        _cap(nivel, techo, ORDEN)


# ---------------------------------------------------------------------------
# EL CALENDARIO SE HA IDO
# ---------------------------------------------------------------------------


def test_el_dia_de_la_semana_no_cambia_nada(cfg):
    """El test que tiene que fallar el día que alguien reponga el calendario.

    Con los mismos días desde la última intensa, la recomendación de un martes
    tiene que ser idéntica a la de un sábado. Antes no lo era ni de lejos: el
    sábado partía de 'intensa', el domingo de 'media' y el resto de la semana el
    sistema no abría la boca.
    """
    niveles = set()
    for i in range(7):
        dia = LUNES + timedelta(days=i)
        rec = recommend_bike(cfg, _senales(dia, dias_desde=DIAS_INTENSA), "green")
        assert rec.applies, f"{rec.day_name} sin recomendación"
        niveles.add(rec.level)
    assert niveles == {"intensa"}, f"el día de la semana sigue decidiendo: {niveles}"


def test_un_miercoles_tambien_se_aconseja(cfg):
    """«Si salgo un miércoles, quiero consejo el miércoles.» Dicho literal.

    En el histórico real hay siete salidas en miércoles y el sistema no dijo
    nada en ninguna de las siete.
    """
    rec = recommend_bike(cfg, _senales(MIERCOLES, dias_desde=DIAS_INTENSA), "green")
    assert rec.applies
    assert rec.day_name == "wednesday"
    assert rec.text().startswith("Bici:")


# ---------------------------------------------------------------------------
# Las tres bandas, y la de arriba que NO es monótona
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dias,esperado",
    [
        # `dias=0` no se prueba porque no existe: la salida de hoy se excluye a
        # propósito (ver `test_la_salida_de_hoy_no_entra_en_su_propio_consejo`),
        # así que los días desde la última intensa valen 1 como mínimo.
        (1, "suave"),
        (P_BAJO - 1, "suave"),
        (P_BAJO, "intensa"),
        (P_ALTO - 1, "intensa"),
        (P_ALTO, "media"),
        (40, "media"),
        (DIAS_PARON_MAX, "media"),
    ],
)
def test_las_tres_bandas_y_sus_fronteras(cfg, dias, esperado):
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=dias), "green")
    assert rec.baseline == esperado, rec.baseline_why


def test_volver_de_un_paron_largo_no_pide_series(cfg):
    """La banda alta baja a 'media', y no es un descuido: es el arreglo.

    La primera versión de esto era monótona -cuantos más días, más duro- y con
    datos reales se clavó en INTENSA seis semanas seguidas durante un bloque
    suave, porque los días desde la última intensa crecen sin tope. Habría
    recomendado intensa en 52 de 88 salidas (59%) contra una tasa real del 26%.

    Volver de dos meses parado pidiendo series es exactamente el consejo que no
    se le puede dar a una espalda con una hernia L4-L5: ahí toca volumen antes
    que carga.

    EL `assert rec.applies` NO SOBRA, Y ESTE TEST ESTUVO VACÍO POR NO TENERLO
    -------------------------------------------------------------------------
    La lista de días llegaba hasta 180, y con 180 días desde la última intensa
    el resto del historial de prueba se sale de la ventana: no quedan huecos
    suficientes, no hay punto de partida y `level` vale 'descanso'. 'descanso'
    no es 'intensa', así que la comprobación pasaba -y habría pasado con la
    banda alta rota, porque no llegaba a evaluarla-. Un test que se cumple
    porque el sistema no contestó no está comprobando la respuesta.
    """
    for dias in (P_ALTO, 30, 45, 60, 90, DIAS_PARON_MAX):
        rec = recommend_bike(cfg, _senales(LUNES, dias_desde=dias), "green")
        assert rec.applies, (
            f"con {dias} días de parón el historial de prueba se queda sin base "
            f"({rec.skip_reason}), así que este caso no está mirando la banda "
            f"alta. El tope es {DIAS_PARON_MAX} días"
        )
        assert rec.level != "intensa", f"con {dias} días de parón: {rec.baseline_why}"


def test_al_dia_siguiente_de_una_intensa_no_se_pide_otra(cfg):
    """El otro extremo. El calendario lo hacía una vez en el histórico real."""
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=1), "green")
    assert rec.level == "suave", rec.baseline_why


def test_el_por_que_del_punto_de_partida_queda_escrito(cfg):
    """El baseline ya no es una constante que se pueda ir a mirar al YAML.

    Cambia cada día según el histórico, así que sin esto no hay manera de
    reconstruir dentro de un mes por qué el sistema dijo lo que dijo.
    """
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=DIAS_INTENSA), "green")
    assert rec.baseline_why
    assert f"{DIAS_INTENSA} días" in rec.baseline_why
    assert "huecos" in rec.baseline_why
    assert rec.to_dict()["baseline_why"] == rec.baseline_why


def test_la_salida_de_hoy_no_entra_en_su_propio_consejo(cfg):
    """El consejo se emite por la mañana: hoy todavía no se ha salido.

    Si la salida de hoy contara, `dias_desde` valdría 0 siempre que se sale y el
    sistema se recomendaría 'suave' a sí mismo para justificar lo ya hecho.
    """
    sig = _senales(LUNES, dias_desde=DIAS_INTENSA)
    sig.rides.append(
        ClassifiedRide(
            ride=Ride(date=LUNES, duration_s=7200),
            level="intensa",
            source="test",
            load=100.0,
            load_estimated=False,
        )
    )
    rec = recommend_bike(cfg, sig, "green")
    assert f"{DIAS_INTENSA} días" in rec.baseline_why


# ---------------------------------------------------------------------------
# Cuando NO hay base no se inventa una, y se dice
# ---------------------------------------------------------------------------


def test_sin_historico_no_se_inventa_un_punto_de_partida(cfg):
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=None), "green")
    assert not rec.applies
    assert rec.skip_reason and "intensa" in rec.skip_reason


def test_con_pocos_huecos_tampoco(cfg):
    """`min_gaps: 4`. Con menos, los percentiles son dos puntos sueltos."""
    rec = recommend_bike(cfg, _senales(LUNES, 7, huecos=[5, 9]), "green")
    assert not rec.applies
    assert "huecos" in rec.skip_reason


def test_si_los_percentiles_salen_iguales_se_niega_a_aconsejar(cfg):
    """La banda que desaparece sin avisar, que es el fallo peligroso.

    Con todos los huecos iguales -salir intensa cada 7 días clavados- los dos
    percentiles valen lo mismo, sean los que sean, y la condición
    `p_bajo <= días < p_alto` no se cumple NUNCA. La
    banda intermedia es la única que dice 'intensa', así que el sistema no
    volvería a proponer una en su vida y nada lo indicaría: seguiría alternando
    'suave' y 'media' con toda naturalidad.
    """
    rec = recommend_bike(cfg, _senales(LUNES, 7, huecos=[7, 7, 7, 7, 7]), "green")
    assert not rec.applies
    assert "iguales" in rec.skip_reason
    assert "intensa" in rec.skip_reason


def test_el_dia_sin_base_se_dice_en_el_mensaje(cfg):
    """`applies=False` ya NO es un no evento, y por eso ahora ocupa una línea.

    Antes significaba "hoy es miércoles y aquí no se habla", que efectivamente
    no había que decir. Ahora solo puede significar "no he podido calcular tu
    punto de partida", y callarse eso es el sistema degradándose en silencio,
    que es el fallo que este proyecto lleva meses persiguiendo.
    """
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=None), "green")
    assert rec.skip_visible
    assert rec.se_muestra
    assert rec.text(), "un día sin base tiene que decir que no la tiene"
    assert rec.skip_reason in rec.text()


def test_sin_base_las_notas_de_contexto_sobreviven(cfg):
    """Lo que se hizo ayer no depende del punto de partida.

    Perder esa línea el día que el histórico no llega para calcular las bandas
    sería castigar al mensaje por un problema que no es suyo.
    """
    sig = _senales(LUNES, dias_desde=None)
    sig.values["yesterday_ride_level"] = "intensa"
    rec = recommend_bike(cfg, sig, "green")
    assert not rec.applies
    assert any("ayer" in n for n in rec.texto_notas())


def test_apagar_la_recomendacion_no_saca_linea(cfg):
    """El otro sabor de `applies=False`, y este sí se calla.

    Apagarla en el config es una decisión tomada a conciencia, no una
    degradación: repetir todos los días que está apagada sería ruido.
    """
    import copy

    raw = copy.deepcopy(cfg.raw)
    raw["cycling"]["recommendation"]["enabled"] = False
    rec = recommend_bike(raw, _senales(LUNES, dias_desde=DIAS_INTENSA), "green")
    assert not rec.applies
    assert not rec.skip_visible
    assert not rec.se_muestra
    assert rec.text() == ""


# ---------------------------------------------------------------------------
# El config del punto de partida revienta, no se degrada
# ---------------------------------------------------------------------------


def test_sin_el_bloque_en_el_config_no_hay_defecto(cfg):
    """El defecto sería una constante inventada. Eso es lo que se acaba de quitar."""
    import copy

    raw = copy.deepcopy(cfg.raw)
    del raw["cycling"]["recommendation"]["baseline_from_gaps"]
    with pytest.raises(BikeConfigError, match="baseline_from_gaps"):
        recommend_bike(raw, _senales(LUNES, dias_desde=DIAS_INTENSA), "green")


def test_un_nivel_de_banda_fuera_de_intensity_order_revienta(cfg):
    import copy

    raw = copy.deepcopy(cfg.raw)
    raw["cycling"]["recommendation"]["baseline_from_gaps"]["level_between"] = "brutal"
    with pytest.raises(BikeConfigError, match="intensity_order"):
        recommend_bike(raw, _senales(LUNES, dias_desde=DIAS_INTENSA), "green")


@pytest.mark.parametrize("falta", ["window_days", "min_gaps", "percentile_low",
                                   "percentile_high", "level_above_high"])
def test_una_clave_que_falta_revienta_en_vez_de_coger_un_defecto(cfg, falta):
    """Se leen con corchetes a propósito.

    Un `.get(clave, defecto)` aquí convertiría una errata en el YAML en un
    sistema que aconseja con otros números que los escritos, sin decir nada.
    """
    import copy

    raw = copy.deepcopy(cfg.raw)
    del raw["cycling"]["recommendation"]["baseline_from_gaps"][falta]
    with pytest.raises(KeyError):
        recommend_bike(raw, _senales(LUNES, dias_desde=DIAS_INTENSA), "green")


# ---------------------------------------------------------------------------
# El techo del semáforo: el ÚNICO recorte
# ---------------------------------------------------------------------------


def test_en_rojo_no_sale_la_intensa(cfg):
    """Sin techo, un rojo se leería como un día cualquiera."""
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=DIAS_INTENSA), "red")
    assert rec.baseline == "intensa"
    assert rec.level == "descanso"


def test_el_recorte_del_semaforo_se_explica_y_no_solo_se_aplica(cfg):
    """Un 'descanso' a secas no se puede discutir ni auditar.

    El motivo es lo que separa una recomendación de una orden, y lo que permite
    mirar atrás dentro de un mes y entender por qué ese día no se salió.
    """
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=DIAS_INTENSA), "red")
    assert rec.downgrades, "un recorte sin motivo no se puede auditar"
    desde, hasta, motivo = rec.downgrades[0]
    assert (desde, hasta) == ("intensa", "descanso")
    assert "rojo" in motivo.lower()
    assert "descanso" in rec.text().lower()


def test_en_ambar_baja_a_suave_pero_no_a_descanso(cfg):
    """El ámbar recorta, no cancela: `actions.amber.bike_max` es 'suave'."""
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=DIAS_INTENSA), "amber")
    assert rec.level == "suave"


def test_en_verde_se_queda_como_estaba(cfg):
    """El techo verde es 'intensa', o sea que no hay techo efectivo.

    Importa comprobarlo: si el verde recortara, el sistema estaría siempre
    frenando y la progresión de bici no llegaría nunca arriba.
    """
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=DIAS_INTENSA), "green")
    assert rec.level == "intensa"
    assert not rec.downgrades


def test_el_rojo_tambien_baja_un_punto_de_partida_medio(cfg):
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=40), "red")
    assert rec.baseline == "media"
    assert rec.level == "descanso"


# ---------------------------------------------------------------------------
# El recuento CUENTA. No recorta. Nunca. Y ya ni siquiera vive aquí.
# ---------------------------------------------------------------------------
#
# Estos tests son la frontera entre el sistema de antes y el de ahora, y están
# escritos al revés que los que sustituyen: antes se comprobaba que el freno
# saltara, y ahora se comprueba que NO salte por mucho que se le cargue la
# semana. Los cuatro frenos por recuento que había -`weekly_limit`,
# `on_budget_exhausted`, `max_intense_rides_per_weekend` y
# `require_green_for_intense`- ya no existen.
#
# El recuento se ha ido además de las notas de la bici a su propia línea del
# mensaje (ver el comentario largo en `_notas_de_contexto`): dependía del orden
# en que se construían dos objetos y eso lo podía hacer desaparecer en silencio.
# Lo que sigue aquí es lo único que le queda a este módulo del recuento: que por
# alto que sea, NO toca el nivel.
#
# Lo que queda como recorte es uno solo: el techo del semáforo. Ese sí puede
# bajar el nivel, porque sale de lo que mide el cuerpo -HRV, sueño, pulso de
# reposo, carga de Garmin- y no de una hoja de cálculo.


def _con_recuento(day: date, used: int, unknown: int = 0) -> Signals:
    from app.engine.signals import IntensityCount

    s = _senales(day, dias_desde=DIAS_INTENSA)
    s.intense_count = IntensityCount(
        used=used, detail=[], week_start=day, unknown=unknown
    )
    return s


@pytest.mark.parametrize("hechas", [0, 1, 3, 4, 7, 12])
def test_el_recuento_no_baja_el_nivel_por_alto_que_sea(cfg, hechas):
    """La semana de viaje: siete días de bici seguidos y el sábado sigue libre.

    Antes, de la cuarta sesión fuerte en adelante el sábado salía 'suave' con el
    motivo "presupuesto agotado". Eso es el sistema decidiendo qué se puede
    hacer, y es justo lo que se ha quitado: el sistema registra y se adapta.
    """
    rec = recommend_bike(cfg, _con_recuento(LUNES, used=hechas), "green")
    assert rec.level == "intensa", [d[2] for d in rec.downgrades]
    assert not rec.downgrades


def test_el_recuento_ya_no_es_una_nota_de_la_bici(cfg):
    """Se mudó al mensaje, y aquí no puede quedar una copia.

    Dos sitios que dicen el mismo número es el estado del que se viene: uno de
    los dos deja de actualizarse y nadie se entera hasta que los dos no cuadran.
    """
    rec = recommend_bike(cfg, _con_recuento(LUNES, used=5), "green")
    assert not rec.downgrades
    assert rec.texto_notas() == [], rec.texto_notas()
    assert rec.level == "intensa"


def test_el_semaforo_sigue_mandando_por_encima_del_recuento(cfg):
    """Quitar los frenos de cuenta no puede haber tocado el freno del cuerpo.

    Es el reverso exacto del cambio: el recuento no recorta NUNCA, y el ámbar
    recorta SIEMPRE. Si al quitar los cuatro frenos se hubiera llevado por
    delante el techo del semáforo, el sistema habría pasado de frenar de más a
    no frenar nada, que es bastante peor.
    """
    rec = recommend_bike(cfg, _con_recuento(LUNES, used=9), "amber")
    assert rec.level == "suave"
    assert rec.downgrades, "el ámbar tiene que seguir explicando por qué recorta"
    assert "ámbar" in rec.downgrades[0][2].lower()


def test_las_notas_no_llevan_tono_de_reprimenda(cfg):
    """«El sistema informa y se adapta, no juzga.» Dicho literal del usuario.

    Esto no es cosmética. Una nota que regaña convierte el contexto en una
    prescripción por la puerta de atrás: no frena el código, frena el que lo
    lee. Y la semana de más carga es precisamente la semana en que menos falta
    hace que nadie te riña.
    """
    sig = _senales(DOMINGO, dias_desde=1)
    sig.values["yesterday_ride_level"] = "intensa"
    rec = recommend_bike(cfg, sig, "green")
    junto = " ".join(rec.texto_notas()).lower()
    assert junto, "el test no vale nada si no hay notas que mirar"
    for palabra in (
        "demasiad", "exceso", "excedid", "deberías", "cuidado", "ojo",
        "agotado", "límite", "te pasas", "de más",
    ):
        assert palabra not in junto, f"tono de reproche: '{palabra}' en {junto!r}"


def test_sin_contexto_no_hay_nota_ni_hueco(cfg):
    """Un día sin nada que contar no puede sacar una línea vacía ni un "None"."""
    rec = recommend_bike(cfg, _senales(LUNES, dias_desde=DIAS_INTENSA), "green")
    assert rec.texto_notas() == []
    assert rec.level == "intensa"


# ---------------------------------------------------------------------------
# La ventana del fin de semana contaba el domingo de la semana pasada
# ---------------------------------------------------------------------------
#
# El filtro era `<= 6 días`, y un sábado el domingo anterior cae exactamente a
# 6. El número que se enseña tiene que ser verdad aunque no decida nada; si
# acaso más, porque un dato que no decide es un dato que nadie va a ir a
# comprobar.


def _con_salidas(day: date, fechas: list[date]) -> Signals:
    s = Signals(day=day)
    s.rides = [
        ClassifiedRide(
            ride=Ride(date=d, duration_s=7200),
            level="intensa",
            source="test",
            load=100.0,
            load_estimated=False,
        )
        for d in fechas
    ]
    return s


def test_el_domingo_pasado_no_es_este_fin_de_semana(cfg):
    from app.engine.bike_advisor import _intense_rides_this_weekend

    domingo_pasado = SABADO - timedelta(days=6)
    assert domingo_pasado.weekday() == 6
    sig = _con_salidas(SABADO, [domingo_pasado])
    assert _intense_rides_this_weekend(sig, cfg.raw["cycling"]) == 0


def test_el_sabado_de_ayer_si_es_este_fin_de_semana(cfg):
    from app.engine.bike_advisor import _intense_rides_this_weekend

    sig = _con_salidas(DOMINGO, [SABADO])
    assert _intense_rides_this_weekend(sig, cfg.raw["cycling"]) == 1


def test_la_salida_del_fin_de_semana_se_cuenta_pero_no_recorta(cfg):
    """El freno que había aquí, `max_intense_rides_per_weekend`, ya no está.

    Y aparte de sobrar, contaba mal: solo era alcanzable los sábados -el punto
    de partida del domingo era 'media' y el bloque entero se saltaba- y en los
    sábados miraba al domingo de la semana anterior.
    """
    sig = _senales(DOMINGO, dias_desde=1)  # la última intensa fue ayer, sábado
    rec = recommend_bike(cfg, sig, "green")
    assert rec.level == "suave", [d[2] for d in rec.downgrades]
    assert not rec.downgrades
    nota = next(n for n in rec.texto_notas() if "fin de semana" in n)
    assert nota == "llevas 1 salida intensa este fin de semana", nota
    assert nota == "llevas 1 salida intensa este fin de semana", nota
    assert "(s)" not in nota, "esto lo lee una persona, no un log"


def test_la_intensa_de_ayer_avisa_y_ademas_mueve_el_punto_de_partida(cfg):
    """`no_consecutive_intense` era el único de los tres que sí disparaba.

    108 veces en la rejilla de 648 combinaciones. Al quitarlo, la intensa de
    ayer pasó a ser solo una nota; ahora vuelve a tener efecto sobre el nivel,
    pero por el camino honesto y no por una regla escrita a mano: un día desde
    la última intensa cae por debajo del percentil bajo de los huecos propios,
    así que la banda baja da 'suave'. La nota sigue saliendo, porque el número
    que se
    enseña es lo que permite discutir el consejo.
    """
    sig = _senales(LUNES, dias_desde=1)
    sig.values["yesterday_ride_level"] = "intensa"
    rec = recommend_bike(cfg, sig, "green")
    assert rec.level == "suave"
    assert not rec.downgrades, "sigue sin haber recorte: el nivel sale del histórico"
    assert any("ayer" in n for n in rec.texto_notas())
