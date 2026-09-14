"""Escritura en Hevy: el módulo más peligroso del proyecto.

`PUT /v1/routines/{id}` REEMPLAZA la rutina entera. Una escritura mal
construida no la degrada: la sustituye. Por eso aquí no se prueba solo que el
camino feliz funcione, sino sobre todo que los frenos muerdan:

  - el interruptor general corta antes de tocar la red,
  - sin copia verificada no se escribe,
  - una escritura interrumpida deja rastro,
  - y el `superset_id` sobrevive al viaje de ida y vuelta.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from app.integrations import hevy
from app.integrations.hevy import (
    Backup,
    HevyClient,
    HevyError,
    build_routine_payload,
    cuerpo_para_put,
    first_backup_of_day,
    latest_backup,
    payload_diff,
    pending_marker,
    read_pending,
    save_backup,
)

from tests.conftest import FakeHTTP, FakeResponse


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


@dataclass
class SesionFalsa:
    """Lo mínimo de `BuiltSession` que mira `build_routine_payload`.

    `notes` ES UNA LISTA, Y ESA LÍNEA COSTÓ UNA ESCRITURA
    -----------------------------------------------------
    Aquí ponía `notes: str | None = None`. En `BuiltSession` -la de verdad- el
    campo es `list[str]`. Así que este doble tenía el tipo bueno y el original el
    malo, y todos los tests de construcción del cuerpo probaban una forma que el
    motor no produce nunca: el `"notes": []` que salía a la red no lo vio nadie
    hasta que Hevy contestó que no, el 2026-09-14 a las nueve de la mañana.

    Un doble que no se parece al original no prueba la integración, prueba el
    doble. Por eso ahora el tipo es el mismo que el real, y por eso hay abajo un
    test que compara las anotaciones de los dos campo por campo: el día que
    `BuiltSession` cambie, este fichero se pone rojo en vez de quedarse mintiendo
    en verde.

    Y ESE TEST, NADA MÁS ESCRIBIRLO, ENCONTRÓ OTROS DOS
    ---------------------------------------------------
    `notes` era el que había costado la escritura, pero no estaba solo:

        routine_key   el doble decía `str`, el original es `str | None`
        title         el doble decía `str | None`, el original es `str`

    Los dos al revés, y los dos con consecuencias. El primero escondía que la
    clave puede ser nula, que es justo el último eslabón de la cadena de
    reservas del título: con él nulo, el cuerpo salía con `"title": null` y Hevy
    contesta 400 (medido). El segundo hacía lo contrario -probar un `title=None`
    que el motor no construye nunca- y así el test de la reserva del config
    parecía cubrir un camino que en realidad no se recorría por donde él creía.

    Tres campos, tres divergencias, un solo test para encontrarlas. El fallo no
    era `notes`: era que nada comparaba el doble con el original.
    """

    routine_key: str | None = "dia_1"
    title: str = "Día 1"
    notes: list[str] = field(default_factory=list)
    exercises: list[dict[str, Any]] = field(default_factory=list)


def ejercicio(**kwargs) -> dict[str, Any]:
    base = {
        "key": "hip_thrust_barra",
        "name": "Hip thrust con barra",
        "template_id": "AAAA1111",
        "rest_seconds": 90,
        "sets": [
            {"type": "warmup", "reps": 10, "weight_kg": 20},
            {"type": "normal", "reps": 8, "weight_kg": 60},
        ],
    }
    base.update(kwargs)
    return base


def cliente(tmp_path: Path, respuestas=None, write_enabled=True) -> tuple[HevyClient, FakeHTTP]:
    doble = FakeHTTP(respuestas or [])
    c = HevyClient(
        api_key="clave-de-prueba",
        data_root=tmp_path,
        write_enabled=write_enabled,
    )
    c._client = lambda: doble  # type: ignore[method-assign]
    return c, doble


REMOTO = {
    "title": "Día 1",
    "notes": None,
    "exercises": [
        {
            "exercise_template_id": "AAAA1111",
            "title": "Hip thrust con barra",
            "sets": [
                {"type": "warmup", "reps": 10, "weight_kg": 20},
                {"type": "normal", "reps": 8, "weight_kg": 55},
            ],
        }
    ],
}


# El cuerpo válido más pequeño que existe. Los tests que no van del CONTENIDO
# -que si se poda una copia, que si la red se cae a mitad- usan éste.
#
# Antes usaban `{"routine": {"exercises": []}}`, que es un cuerpo que Hevy
# RECHAZA por dos motivos a la vez: sin título contesta 400 «Required» y sin
# ejercicios otro 400. O sea que media docena de tests de escritura afirmaban
# cosas sobre el resultado de un PUT que en la realidad nunca habría salido bien.
# No es que estuvieran mal escritos: es que hasta ahora nada comprobaba el
# cuerpo, así que daba igual lo que se le pasara. En cuanto el contrato mira,
# esos tests se caen, que es exactamente para lo que sirve el contrato.
def cuerpo_valido() -> dict[str, Any]:
    return {
        "routine": {
            "title": "Día 1",
            "notes": None,
            "exercises": [
                {
                    "exercise_template_id": "AAAA1111",
                    "sets": [{"type": "normal", "reps": 8, "weight_kg": 60}],
                }
            ],
        }
    }


# ---------------------------------------------------------------------------
# Construcción del cuerpo del PUT
# ---------------------------------------------------------------------------


def test_el_cuerpo_lleva_titulo_y_ejercicios_indexados():
    s = SesionFalsa(exercises=[ejercicio(), ejercicio(key="otro", template_id="BBBB2222")])
    body = build_routine_payload(s, {})
    assert body["routine"]["title"] == "Día 1"
    assert [e["index"] for e in body["routine"]["exercises"]] == [0, 1]
    assert [s_["index"] for s_ in body["routine"]["exercises"][0]["sets"]] == [0, 1]


def test_el_superset_id_se_copia_tal_cual():
    """Omitirlo deshace las superseries en la app, en silencio."""
    s = SesionFalsa(exercises=[ejercicio(superset_id=7)])
    body = build_routine_payload(s, {})
    assert body["routine"]["exercises"][0]["superset_id"] == 7


def test_sin_superset_va_none_explicito_no_ausente():
    s = SesionFalsa(exercises=[ejercicio()])
    ex = build_routine_payload(s, {})["routine"]["exercises"][0]
    assert "superset_id" in ex
    assert ex["superset_id"] is None


def test_los_campos_que_no_aplican_van_a_null_y_no_se_omiten():
    """Así el cuerpo tiene siempre la misma forma y el diff no miente."""
    s = SesionFalsa(exercises=[ejercicio()])
    serie = build_routine_payload(s, {})["routine"]["exercises"][0]["sets"][0]
    assert serie["distance_meters"] is None
    assert serie["duration_seconds"] is None
    assert serie["custom_metric"] is None


def test_el_tipo_de_serie_se_normaliza_a_minusculas():
    s = SesionFalsa(exercises=[ejercicio(sets=[{"type": "WARMUP", "reps": 10}])])
    serie = build_routine_payload(s, {})["routine"]["exercises"][0]["sets"][0]
    assert serie["type"] == "warmup"


def test_una_serie_sin_tipo_es_normal():
    s = SesionFalsa(exercises=[ejercicio(sets=[{"reps": 10}])])
    serie = build_routine_payload(s, {})["routine"]["exercises"][0]["sets"][0]
    assert serie["type"] == "normal"


def test_el_titulo_cae_a_la_definicion_del_config_si_la_sesion_no_lo_trae():
    """La reserva se ejercita con un título VACÍO, que es lo que puede pasar.

    Antes se pasaba `title=None`, y `BuiltSession.title` está declarado `str`:
    o sea que el caso probado no lo produce el motor. La cadena de reservas se
    recorre igual con la cadena vacía, que sí es alcanzable -`str(routine.get(
    "title", routine_key))` sobre un YAML con el título en blanco- y además es
    el tipo bueno.
    """
    s = SesionFalsa(title="", exercises=[])
    cfg = {"routines": {"dia_1": {"title": "Día 1 (del YAML)"}}}
    assert build_routine_payload(s, cfg)["routine"]["title"] == "Día 1 (del YAML)"


def test_sin_titulo_por_ninguna_via_se_para_en_vez_de_mandar_un_nulo():
    """El gemelo del fallo de `notes`, en el campo de al lado.

    `BuiltSession.routine_key` es `str | None` y era el último eslabón de la
    cadena de reservas del título. Con él nulo el cuerpo salía con
    `"title": null`, que el contrato daba por bueno -comprobaba «texto o nada»-
    y Hevy rechaza con un 400 (medido). Ahora se para en la construcción, que es
    donde todavía se puede decir qué sesión venía sin nombre.
    """
    s = SesionFalsa(title="", routine_key=None, exercises=[])
    with pytest.raises(HevyError, match="no tiene título por ninguna vía"):
        build_routine_payload(s, {})


def test_un_titulo_nulo_no_pasa_el_contrato():
    """`None` vale en `notes` y NO vale en `title`. No son la misma comprobación."""
    with pytest.raises(HevyError, match="título de la rutina"):
        cuerpo_para_put({"routine": {"title": None, "notes": None, "exercises": []}})


def test_el_cuerpo_es_serializable_a_json():
    s = SesionFalsa(exercises=[ejercicio()])
    json.dumps(build_routine_payload(s, {}))


def test_el_cuerpo_construido_contra_el_config_real(cfg):
    """Comprobación de integración: la rutina real produce un cuerpo completo."""
    routine = cfg.raw["routines"]["dia_1"]
    s = SesionFalsa(
        routine_key="dia_1",
        title=routine.get("title"),
        exercises=[
            {**ex, "sets": ex["sets"]} for ex in routine["exercises"]
        ],
    )
    body = build_routine_payload(s, cfg)
    assert body["routine"]["exercises"]
    for ex in body["routine"]["exercises"]:
        assert ex["exercise_template_id"], "Hevy rechaza un ejercicio sin template_id"
        assert ex["sets"], "un ejercicio sin series no es un ejercicio"


# ---------------------------------------------------------------------------
# Las notas de la rutina: el fallo del 2026-09-14
# ---------------------------------------------------------------------------
#
# `BuiltSession.notes` es `list[str]` y `build_routine_payload` la pasaba tal
# cual, así que el PUT salía con `"notes": []`. Hevy contesta
# `400 Expected string, received array` -medido, ver
# `scripts/sondeo_notas_hevy.py`- y la rutina se quedó como estaba desde el 8 de
# septiembre.
#
# Ninguno de los tests de arriba podía verlo, porque el doble declaraba
# `notes: str | None`. Éstos sí.


def test_las_notas_de_la_sesion_viajan_como_texto_y_no_como_lista():
    """El fallo exacto del 2026-09-14, con el tipo que usa el motor de verdad."""
    s = SesionFalsa(notes=["cuida la lumbar", "sin prisa"], exercises=[ejercicio()])
    notas = build_routine_payload(s, {})["routine"]["notes"]
    assert isinstance(notas, str), (
        f"las notas salen como {type(notas).__name__}; Hevy contesta "
        f"400 «Expected string, received array» y no escribe nada"
    )
    assert "cuida la lumbar" in notas and "sin prisa" in notas


def test_sin_notas_va_none_y_no_una_lista_vacia_ni_una_cadena_vacia():
    """`[]` es el 400 y `""` es una nota vacía puesta a propósito. Ninguna de las dos."""
    s = SesionFalsa(notes=[], exercises=[ejercicio()])
    assert build_routine_payload(s, {})["routine"]["notes"] is None


def test_una_nota_que_ya_viene_en_texto_se_respeta():
    s = SesionFalsa(notes="una sola nota", exercises=[])
    assert build_routine_payload(s, {})["routine"]["notes"] == "una sola nota"


def test_unas_notas_de_un_tipo_imposible_revientan_en_vez_de_str():
    """Un `str()` a ciegas escribiría en la rutina la repr de un objeto."""
    s = SesionFalsa(exercises=[])
    s.notes = {"no": "esto"}  # type: ignore[assignment]
    with pytest.raises(HevyError, match="notas de la sesión"):
        build_routine_payload(s, {})


def test_el_doble_de_sesion_declara_los_mismos_tipos_que_BuiltSession():
    """El guardián de todo lo de arriba: que el doble se parezca al original.

    ESTE ES EL TEST QUE FALTABA. El fallo del 2026-09-14 no fue que `notes`
    tuviera el tipo malo -eso es un descuido de una línea-, fue que la batería no
    podía enterarse: `SesionFalsa` declaraba `notes: str | None` y `BuiltSession`
    `list[str]`, así que todos los tests de construcción del cuerpo ejercitaban
    una forma que el motor no produce nunca. Un doble que no se parece al
    original no prueba la integración, prueba el doble.

    Se comparan las ANOTACIONES y no los valores: lo que se vigila es que el día
    que `BuiltSession` cambie un tipo, este fichero se ponga rojo en vez de
    seguir en verde midiendo otra cosa. Sólo se miran los campos que el doble
    dice tener -no tiene por qué copiar `BuiltSession` entero, sólo lo que
    `build_routine_payload` mira- pero los que tiene, con el tipo bueno.
    """
    from app.engine.session_builder import BuiltSession

    reales = BuiltSession.__annotations__
    falsas = SesionFalsa.__annotations__

    desconocidos = set(falsas) - set(reales)
    assert not desconocidos, (
        f"el doble declara campos que `BuiltSession` no tiene: {sorted(desconocidos)}. "
        f"O sobran, o el original los perdió y el doble se quedó con ellos."
    )

    discrepan = {
        campo: (reales[campo], falsas[campo])
        for campo in falsas
        if str(reales[campo]) != str(falsas[campo])
    }
    assert not discrepan, (
        f"el doble y `BuiltSession` no declaran lo mismo: {discrepan}. "
        f"Con `notes` esto costó la escritura del 2026-09-14: el doble tenía "
        f"`str | None`, el original `list[str]`, y los tests probaban un cuerpo "
        f"que el motor no construye."
    )


# ---------------------------------------------------------------------------
# El contrato de tipos de `cuerpo_para_put`
# ---------------------------------------------------------------------------
#
# La lista blanca comprobaba NOMBRES, que es la mitad del contrato. Por la otra
# mitad se fue la mañana del 2026-09-14.
#
# Lo que esta sección vigila es la parte estricta -texto donde va texto- y, sobre
# todo, la parte que Hevy NO protege: los campos numéricos se coercionan con el
# `Number()` de JavaScript, así que `weight_kg: []` se escribiría como cero kilos
# sin que nadie diga una palabra. Ahí el único freno es local.


def test_unas_notas_en_lista_no_salen_a_la_red():
    with pytest.raises(HevyError, match="notas de la rutina"):
        cuerpo_para_put({"routine": {"title": "x", "notes": [], "exercises": []}})


def test_un_titulo_que_no_es_texto_no_sale_a_la_red():
    with pytest.raises(HevyError, match="título de la rutina"):
        cuerpo_para_put({"routine": {"title": ["x"], "notes": None, "exercises": []}})


def test_unas_notas_de_ejercicio_en_lista_tampoco():
    with pytest.raises(HevyError, match="notas del ejercicio 0"):
        cuerpo_para_put(
            {
                "routine": {
                    "title": "x",
                    "notes": None,
                    "exercises": [{"exercise_template_id": "A", "notes": [],
                                   "sets": []}],
                }
            }
        )


def test_el_error_de_tipo_dice_QUE_CAMPO_es_cosa_que_hevy_no_hace():
    """La razón de que esta comprobación no sobre aunque Hevy valide.

    Medido contra la API real: el 400 entero es `Expected string, received
    array`. Ni la ruta, ni el nombre del campo, ni el índice del ejercicio. O
    sea que el 2026-09-14, incluso con el texto del 400 bien guardado -que
    tampoco lo estaba-, el mensaje de la mañana habría dicho que algo de tipo
    array iba donde va texto, sin decir dónde. Aquí se dice.
    """
    with pytest.raises(HevyError) as exc:
        cuerpo_para_put({"routine": {"title": "x", "notes": [], "exercises": []}})
    texto = str(exc.value)
    assert "notas de la rutina" in texto
    assert "list" in texto, "el error tiene que decir qué tipo llegó"


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("weight_kg", []),       # Number([]) == 0: se escribiría 0 kg
        ("reps", True),          # Number(true) == 1: se escribiría 1 repetición
        ("reps", {"a": 1}),
        ("duration_seconds", ["30"]),
    ],
)
def test_un_valor_no_escalar_en_una_serie_se_para_aqui(campo, valor):
    """Hevy NO protege de esto: lo convierte en silencio.

    Está medido (`scripts/sondeo_notas_hevy.py`): `weight_kg: []` no da 400,
    pasa, y vale CERO. `reps: true` pasa, y vale 1. Son los dos casos peores del
    contrato entero -no hay error, hay un número plausible escrito en la rutina-
    y el único sitio donde se pueden parar es éste, antes de enviar.
    """
    with pytest.raises(HevyError, match="serie 0 del ejercicio 0"):
        cuerpo_para_put(
            {
                "routine": {
                    "title": "x",
                    "notes": None,
                    "exercises": [
                        {
                            "exercise_template_id": "A",
                            "sets": [{"type": "normal", campo: valor}],
                        }
                    ],
                }
            }
        )


def test_un_valor_no_escalar_en_un_ejercicio_tambien():
    with pytest.raises(HevyError, match="ejercicio 0 manda"):
        cuerpo_para_put(
            {
                "routine": {
                    "title": "x",
                    "notes": None,
                    "exercises": [
                        {"exercise_template_id": "A", "rest_seconds": [90],
                         "sets": []}
                    ],
                }
            }
        )


def test_un_cuerpo_bien_tipado_pasa_entero():
    """El contrapeso: los frenos de arriba no pueden morder lo que es correcto."""
    salida = cuerpo_para_put(
        {
            "routine": {
                "title": "Día 1",
                "notes": "una nota",
                "exercises": [
                    {
                        "exercise_template_id": "A",
                        "notes": None,
                        "superset_id": None,
                        "rest_seconds": 90,
                        "sets": [{"type": "normal", "reps": 8, "weight_kg": 62.5,
                                  "duration_seconds": None}],
                    }
                ],
            }
        }
    )
    assert salida["routine"]["notes"] == "una nota"
    assert salida["routine"]["exercises"][0]["sets"][0]["weight_kg"] == 62.5


def test_el_motor_y_el_contrato_encajan_con_el_config_real(cfg):
    """El eslabón que no existía: construir con el motor y pasarlo por el contrato.

    Cada mañana se hace exactamente esto -`build_routine_payload` y después
    `cuerpo_para_put`- y no había un solo test que hiciera los dos pasos
    seguidos con la sesión del motor. Los de construcción paraban antes del
    contrato y los del contrato empezaban con diccionarios escritos a mano. El
    `"notes": []` vivía justo en la costura.
    """
    routine = cfg.raw["routines"]["dia_1"]
    s = SesionFalsa(
        routine_key="dia_1",
        title=routine.get("title"),
        notes=["nota del motor"],
        exercises=[{**ex, "sets": ex["sets"]} for ex in routine["exercises"]],
    )
    cuerpo = cuerpo_para_put(build_routine_payload(s, cfg))
    assert cuerpo["routine"]["notes"] == "nota del motor"
    from tests.conftest import _hevy_rechazaria

    assert _hevy_rechazaria(cuerpo) is None, (
        "el cuerpo que produce el motor con el config real lo rechazaría Hevy"
    )


# ---------------------------------------------------------------------------
# La marca de calentamiento
# ---------------------------------------------------------------------------
#
# `set_types.write_warmup_type_to_hevy` estaba en el YAML desde el principio y
# no lo leía nadie: `_set_payload` copiaba el `type` de la serie pasara lo que
# pasara. La opción no decidía nada ni en `true` ni en `false`.
#
# Lo que gobierna ahora es la marca que AÑADE el sistema por su cuenta -hoy, la
# de la heurística de "la primera de cuatro"-. Sin ella, el motor descuenta esa
# serie del cumplimiento y del volumen mientras la app la sigue enseñando como
# efectiva: una discrepancia entre lo que el sistema cuenta y lo que el usuario
# ve, y de las que no dan ningún error.

CUATRO_SIN_MARCAR = [
    {"reps": 10, "weight_kg": 20},
    {"reps": 8, "weight_kg": 60},
    {"reps": 8, "weight_kg": 60},
    {"reps": 8, "weight_kg": 60},
]

SET_TYPES = {
    "source": "api_then_heuristic",
    "heuristic": {"enabled": True, "sets_gte": 4, "count": 1},
    "overrides": {},
}


def tipos(sets, set_cfg, **extra) -> list[str]:
    cfg = {"set_types": {**SET_TYPES, **extra}} if set_cfg else {}
    s = SesionFalsa(exercises=[ejercicio(sets=sets)])
    return [x["type"] for x in build_routine_payload(s, cfg)["routine"]["exercises"][0]["sets"]]


def test_la_serie_que_el_motor_da_por_calentamiento_sale_marcada():
    """El caso que hace falta que llegue a Hevy: cuatro series sin marcar."""
    assert tipos(CUATRO_SIN_MARCAR, True) == ["warmup", "normal", "normal", "normal"]


def test_con_la_opcion_apagada_la_marca_del_motor_no_viaja():
    """El otro lado del interruptor, que es lo que no existía."""
    assert tipos(
        CUATRO_SIN_MARCAR, True, write_warmup_type_to_hevy=False
    ) == ["normal"] * 4


def test_apagar_la_opcion_no_borra_las_marcas_que_ya_traia_la_rutina():
    """La invariante que impide convertir un interruptor en una pérdida de datos.

    Si `false` significara "manda todo como normal", cada mañana el sistema
    borraría en Hevy los calentamientos que el usuario marcó a mano. Es el mismo
    razonamiento del `superset_id`: lo que venía con la rutina se reproduce.
    """
    marcadas = [{"type": "warmup", "reps": 10}, {"type": "normal", "reps": 8}]
    assert tipos(marcadas, True, write_warmup_type_to_hevy=False) == [
        "warmup", "normal",
    ]


def test_la_marca_no_pisa_un_dropset_ni_un_fallo():
    """Solo AÑADE. Una serie que el motor no considera calentamiento conserva
    su tipo, aunque no sea `normal`."""
    sets = [
        {"reps": 10, "weight_kg": 20},
        {"type": "dropset", "reps": 8, "weight_kg": 60},
        {"type": "failure", "reps": 8, "weight_kg": 60},
        {"reps": 8, "weight_kg": 60},
    ]
    assert tipos(sets, True) == ["warmup", "dropset", "failure", "normal"]


def test_sin_seccion_set_types_no_se_inventa_ningun_calentamiento():
    """Coherente con `warmup_flags`: sin la sección no hay heurística, así que
    tampoco hay marca que escribir. Un cuerpo construido con un config a medias
    no puede reetiquetar la rutina del usuario."""
    assert tipos(CUATRO_SIN_MARCAR, False) == ["normal"] * 4


def test_el_config_real_escribe_la_marca(cfg):
    assert cfg.raw["set_types"]["write_warmup_type_to_hevy"] is True


def test_con_las_rutinas_de_hoy_la_opcion_no_cambia_ni_una_serie(cfg):
    """Medido, no supuesto, y escrito para que se note el día que deje de serlo.

    Las 19 series de calentamiento vienen ya declaradas en las rutinas, así que
    la heurística no añade ninguna y los dos lados del interruptor dan el mismo
    cuerpo. Ese es su estado de destino, no un defecto. El día que un ejercicio
    llegue a 4 series sin calentamiento declarado, este test caerá y será la
    señal de que la opción ha empezado a mandar.
    """
    import copy

    for rk, rd in cfg.raw["routines"].items():
        s = SesionFalsa(routine_key=rk, title=rd.get("title"),
                        exercises=[dict(e) for e in rd["exercises"]])
        apagado = copy.deepcopy(cfg)
        apagado.raw["set_types"]["write_warmup_type_to_hevy"] = False
        assert build_routine_payload(s, cfg) == build_routine_payload(s, apagado), (
            f"'{rk}' ya no es indiferente a write_warmup_type_to_hevy: hay algún "
            f"ejercicio de 4+ series sin calentamiento declarado. Bien: la "
            f"opción ha empezado a servir para algo. Actualiza este test."
        )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def test_el_diff_señala_el_cambio_de_peso():
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    lineas = payload_diff(REMOTO, nuevo)
    assert any("55 -> 60" in l for l in lineas)


def test_el_diff_señala_altas_y_bajas():
    nuevo = build_routine_payload(
        SesionFalsa(exercises=[ejercicio(key="nuevo", template_id="ZZZZ9999")]), {}
    )
    lineas = "\n".join(payload_diff(REMOTO, nuevo))
    assert "+ ALTA" in lineas
    assert "- BAJA" in lineas


def test_el_diff_señala_el_cambio_en_el_numero_de_series():
    nuevo = build_routine_payload(
        SesionFalsa(exercises=[ejercicio(sets=[{"type": "normal", "reps": 8, "weight_kg": 55}])]),
        {},
    )
    assert any("2 -> 1 series" in l for l in payload_diff(REMOTO, nuevo))


def test_sin_cambios_el_diff_lo_dice():
    igual = SesionFalsa(
        exercises=[
            ejercicio(
                sets=[
                    {"type": "warmup", "reps": 10, "weight_kg": 20},
                    {"type": "normal", "reps": 8, "weight_kg": 55},
                ]
            )
        ]
    )
    nuevo = build_routine_payload(igual, {})
    assert payload_diff(REMOTO, nuevo) == ["sin cambios respecto a lo que ya hay en Hevy"]


def test_sin_estado_remoto_el_diff_lo_admite_en_vez_de_fingir():
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    lineas = payload_diff(None, nuevo)
    assert "no se pudo leer el estado remoto" in lineas[0]


# ---------------------------------------------------------------------------
# Copias de seguridad
# ---------------------------------------------------------------------------


def test_la_copia_se_guarda_y_se_relee(tmp_path):
    b = save_backup(tmp_path, "r1", REMOTO)
    assert b.path.is_file()
    assert b.payload == REMOTO
    assert json.loads(b.path.read_text(encoding="utf-8"))["routine"] == REMOTO
    assert "1 ejercicios" in b.describe()


def test_una_copia_que_no_se_puede_releer_impide_escribir(tmp_path, monkeypatch):
    """La verificación no es paranoia decorativa.

    Descubrir que la copia no vale en el momento de revertir —que es siempre el
    peor momento— es exactamente lo que este módulo existe para evitar.
    """

    def escritura_corrupta(self, *args, **kwargs):  # noqa: ANN001
        return "{ esto no es json"

    monkeypatch.setattr(Path, "read_text", escritura_corrupta)
    with pytest.raises(HevyError, match="NO se escribe"):
        save_backup(tmp_path, "r1", REMOTO)


def test_una_copia_que_no_coincide_impide_escribir(tmp_path, monkeypatch):
    def otra_cosa(self, *args, **kwargs):  # noqa: ANN001
        return json.dumps({"routine_id": "r1", "taken_at": "2026-01-01T00:00:00",
                           "routine": {"title": "otra cosa"}})

    monkeypatch.setattr(Path, "read_text", otra_cosa)
    with pytest.raises(HevyError, match="no coincide"):
        save_backup(tmp_path, "r1", REMOTO)


def test_latest_backup_devuelve_la_mas_reciente(tmp_path):
    assert latest_backup(tmp_path, "r1") is None
    save_backup(tmp_path, "r1", REMOTO)
    otro = {**REMOTO, "title": "más nuevo"}
    save_backup(tmp_path, "r1", otro)
    ultimo = latest_backup(tmp_path, "r1")
    assert ultimo is not None
    assert ultimo.payload == otro


def test_una_copia_ilegible_no_revienta_la_lectura(tmp_path):
    save_backup(tmp_path, "r1", REMOTO)
    fichero = next((tmp_path / "hevy_backups" / "r1").glob("*.json"))
    fichero.write_text("{roto", encoding="utf-8")
    assert latest_backup(tmp_path, "r1") is None


@pytest.mark.parametrize("contenido", [
    {"routine": {}},                              # sin `taken_at`
    {"taken_at": None, "routine": {}},            # con `taken_at` vacío
    {"taken_at": "ayer por la tarde", "routine": {}},
])
def test_una_copia_sin_fecha_utilizable_tampoco_revienta(tmp_path, contenido):
    """Un JSON válido al que le falta un campo no es un fichero corrupto, así
    que no lo cazaba el `except` de arriba: salía un KeyError pelado. Y esto se
    lee desde `restore`, o sea el peor momento posible para una traza.

    Ahora acaba donde acaban las demás copias inservibles: no la hay. Quien
    llame se encontrará con «no hay ninguna copia», que es una frase.
    """
    save_backup(tmp_path, "r1", REMOTO)
    fichero = next((tmp_path / "hevy_backups" / "r1").glob("*.json"))
    fichero.write_text(json.dumps(contenido), encoding="utf-8")

    assert latest_backup(tmp_path, "r1") is None

    c, _ = cliente(tmp_path, [])
    with pytest.raises(HevyError, match="no hay ninguna copia"):
        c.restore("r1")


def _copia_a_mano(raiz: Path, routine_id: str, cuando: str, payload: dict) -> Path:
    """Escribe una copia con la hora que se le diga.

    `save_backup` sella con `datetime.now()` al segundo, así que dos llamadas
    seguidas caen en el mismo nombre de fichero y la segunda pisa a la primera.
    Para probar «la más antigua del día» hacen falta copias de horas distintas,
    y la única forma honesta de tenerlas es ponerlas.
    """
    carpeta = raiz / "hevy_backups" / routine_id
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / f"{cuando}.json"
    marca = datetime.strptime(cuando, "%Y%m%d-%H%M%S")
    destino.write_text(json.dumps({
        "routine_id": routine_id,
        "taken_at": marca.isoformat(timespec="seconds"),
        "routine": payload,
    }), encoding="utf-8")
    return destino


def test_la_primera_copia_del_dia_no_es_la_ultima(tmp_path):
    """La distinción entera del check-in tardío está aquí.

    Si un día hubo DOS escrituras, `latest_backup` da el estado intermedio: el
    de después de la primera, o sea con la rutina de hoy ya puesta. Revertir a
    eso y anunciarlo como reversión dejaría `Día 1` en Hevy mientras el mensaje
    dice que se ha quitado, que es peor que no revertir: el aviso taparía el
    problema en vez de enseñarlo.
    """
    antes = {**REMOTO, "title": "lo de la semana pasada"}
    enmedio = {**REMOTO, "title": "Día 1, puesto a las 09:00"}
    _copia_a_mano(tmp_path, "r1", "20260914-085900", antes)
    _copia_a_mano(tmp_path, "r1", "20260914-103000", enmedio)

    primera = first_backup_of_day(tmp_path, "r1", date(2026, 9, 14))
    assert primera is not None
    assert primera.payload == antes, (
        "se ha cogido la copia de en medio: revertir a ella deja puesta la "
        "rutina que se quería quitar"
    )
    assert latest_backup(tmp_path, "r1").payload == enmedio


def test_una_copia_de_otro_dia_no_sirve_para_deshacer_hoy(tmp_path):
    """Y NO se cae hacia `latest_backup`, que es la trampa cómoda.

    La copia más reciente de ayer describe el estado anterior a la escritura de
    AYER. Ponerla hoy no deshace nada: cambia la rutina por una tercera cosa que
    no es ni la de hoy ni la de antes de hoy, y encima lo llama reversión.
    """
    _copia_a_mano(tmp_path, "r1", "20260913-090000", {**REMOTO, "title": "de ayer"})

    assert first_backup_of_day(tmp_path, "r1", date(2026, 9, 14)) is None
    assert latest_backup(tmp_path, "r1") is not None, "la de ayer sí está ahí"

    c, doble = cliente(tmp_path, [])
    with pytest.raises(HevyError, match="no hay ninguna copia"):
        c.revert_to_day_start("r1", date(2026, 9, 14))
    assert doble.llamadas == [], "no se ha tocado la red, que es lo importante"


def test_si_la_primera_copia_del_dia_esta_rota_no_se_prueba_con_la_siguiente(tmp_path):
    """La siguiente describe el estado de DESPUÉS de la primera escritura.

    Restaurarla dejaría la rutina de hoy puesta mientras se anuncia que se ha
    quitado. Mejor no poder revertir y decirlo: esa rama tiene su propio aviso,
    y ese aviso dice qué hacer.
    """
    rota = _copia_a_mano(tmp_path, "r1", "20260914-085900", REMOTO)
    _copia_a_mano(tmp_path, "r1", "20260914-103000", {**REMOTO, "title": "en medio"})
    rota.write_text("{roto", encoding="utf-8")

    assert first_backup_of_day(tmp_path, "r1", date(2026, 9, 14)) is None


def test_deshacer_lo_de_hoy_devuelve_la_rutina_anterior(tmp_path):
    """El camino completo, contra el contrato real del PUT.

    `restore` limpia el cuerpo antes de mandarlo -una copia trae `index` y
    `title` por ejercicio, y eso es un 400- y esa limpieza estuvo rota desde el
    primer día porque sus tests usaban un doble que aceptaba cualquier cuerpo.
    Aquí se mira lo que se manda, no solo que se mande.
    """
    antes = {**REMOTO, "title": "lo de la semana pasada"}
    _copia_a_mano(tmp_path, "r1", "20260914-085900", antes)
    _copia_a_mano(tmp_path, "r1", "20260914-103000", {**REMOTO, "title": "Día 1"})

    c, doble = cliente(tmp_path, [FakeResponse(200, {"routine": antes})])
    r = c.revert_to_day_start("r1", date(2026, 9, 14))

    assert r.written is True
    assert "revertida al estado de" in r.reason
    ultima = doble.llamadas[-1]
    assert (ultima["verb"], ultima["url"]) == ("put", "/v1/routines/r1")
    enviado = ultima["json"]["routine"]
    assert enviado["title"] == "lo de la semana pasada"
    for ej in enviado.get("exercises") or []:
        assert "index" not in ej and "title" not in ej, (
            f"se manda un campo que Hevy no acepta en un PUT: {ej}"
        )


# ---------------------------------------------------------------------------
# Marca de escritura en curso
# ---------------------------------------------------------------------------


def test_sin_marca_no_hay_escritura_pendiente(tmp_path):
    assert read_pending(tmp_path) is None


def test_una_marca_ilegible_se_reporta_igualmente(tmp_path):
    marca = pending_marker(tmp_path)
    marca.parent.mkdir(parents=True, exist_ok=True)
    marca.write_text("{roto", encoding="utf-8")
    pendiente = read_pending(tmp_path)
    assert pendiente is not None
    assert "ilegible" in pendiente["note"]


# ---------------------------------------------------------------------------
# write_routine
# ---------------------------------------------------------------------------


def test_el_interruptor_corta_antes_de_tocar_la_red(tmp_path):
    """Un interruptor que hay que acordarse de mirar no es un interruptor."""
    c, doble = cliente(tmp_path, write_enabled=False)
    r = c.write_routine("r1", cuerpo_valido())
    assert not r.written
    assert "write_enabled" in r.reason
    assert doble.llamadas == [], "no debería haberse hecho ni la lectura previa"
    assert not (tmp_path / "hevy_backups").exists()


def test_el_dry_run_hace_la_copia_pero_no_envia_el_put(tmp_path):
    c, doble = cliente(tmp_path, [FakeResponse(200, REMOTO)])
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    r = c.write_routine("r1", nuevo, dry_run=True)
    assert not r.written
    assert r.backup is not None and r.backup.path.is_file()
    assert any("55 -> 60" in l for l in r.diff)
    assert [l["verb"] for l in doble.llamadas] == ["get"]
    assert read_pending(tmp_path) is None


def test_una_escritura_correcta_deja_copia_y_retira_la_marca(tmp_path):
    c, doble = cliente(tmp_path, [FakeResponse(200, REMOTO), FakeResponse(200, {"ok": True})])
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    r = c.write_routine("r1", nuevo)
    assert r.written
    assert r.backup is not None and r.backup.payload == REMOTO
    assert read_pending(tmp_path) is None, "la marca debe retirarse al confirmar"
    assert [l["verb"] for l in doble.llamadas] == ["get", "put"]
    # Lo que viaja NO es el payload tal cual, y dar eso por hecho -que es lo que
    # este test daba por hecho- es lo que dejó la escritura rota hasta el día en
    # que se abrió el interruptor: el payload del motor lleva `index` y `title`,
    # que sirven para el diff y para el mensaje, y que Hevy rechaza con un 400.
    enviado = doble.llamadas[1]["json"]
    assert enviado == cuerpo_para_put(nuevo)
    for ex in enviado["routine"]["exercises"]:
        assert "index" not in ex and "title" not in ex
        for s in ex["sets"]:
            assert "index" not in s


def test_un_put_rechazado_conserva_la_copia(tmp_path):
    """Se llamaba `..._no_borra_la_marca_ni_la_copia`, y era lo que NO hay que hacer.

    Ojo al nombre viejo, porque describía el fallo que amaneció el 2026-09-14:
    con un 400, la marca de «hay una rutina en estado desconocido» se quedaba
    puesta. Y el test no lo comprobaba -sólo miraba la copia-, así que el nombre
    afirmaba un comportamiento que nadie verificaba y que además era el
    equivocado. Un test cuyo título dice más que sus asertos es un sitio donde
    esconderse.

    Lo que se conserva es la COPIA, que es lo correcto: sirve para comparar. La
    marca se retira, y de eso va el test de abajo.
    """
    c, _ = cliente(tmp_path, [FakeResponse(200, REMOTO), FakeResponse(400, text="mal")])
    r = c.write_routine("r1", cuerpo_valido())
    assert not r.written
    assert "400" in (r.error or "")
    assert r.backup is not None and r.backup.path.is_file()


# ---------------------------------------------------------------------------
# La marca: qué la pone y qué la quita
# ---------------------------------------------------------------------------
#
# La marca significa UNA cosa: no se sabe en qué estado quedó la rutina. Todo lo
# que se sepa con certeza -que Hevy dijo que no, que el cuerpo ni salió- no puede
# dejarla puesta, porque un aviso que salta cuando no pasa nada es un aviso que
# se acaba ignorando. Eso es literalmente lo que pasó: la mañana del 2026-09-14
# amaneció con un aviso de escritura a medias que describía una rutina intacta
# desde el 8 de septiembre.


def test_un_400_retira_la_marca_porque_hevy_ha_dicho_que_no(tmp_path):
    """«Te he dicho que no» no es «a saber». Es el fallo del 2026-09-14.

    Hevy valida el cuerpo ANTES de buscar la rutina y antes de tocar nada
    (medido): un 4xx significa que ha mirado el cuerpo, lo ha rechazado y no ha
    aplicado NADA. El estado remoto es el de la copia que se acaba de tomar, y
    eso se sabe. Dejar la marca puesta convertía una certeza en una duda.
    """
    c, _ = cliente(
        tmp_path,
        [FakeResponse(200, REMOTO),
         FakeResponse(400, text='{"error":"Expected string, received array"}')],
    )
    r = c.write_routine("r1", cuerpo_valido())
    assert not r.written
    assert read_pending(tmp_path) is None, (
        "con un 400 se sabe que la rutina está intacta: la marca de «estado "
        "desconocido» sobra y encima tapa a la de verdad el día que la haya"
    )
    assert "no ha aplicado nada" in (r.error or "")
    assert r.http_status == 400


def test_un_500_deja_la_marca_puesta_porque_ahi_no_se_sabe(tmp_path):
    """El contrapeso del test de arriba, y el motivo de que no se borre siempre.

    Un fallo del servidor no dice en qué momento se cayó: puede haber aplicado
    la escritura antes de reventar. Ahí la marca es exactamente lo que hay que
    dejar puesto.
    """
    c, _ = cliente(
        tmp_path, [FakeResponse(200, REMOTO), FakeResponse(500, text="boom")]
    )
    r = c.write_routine("r1", cuerpo_valido())
    assert not r.written
    pendiente = read_pending(tmp_path)
    assert pendiente is not None, "con un 5xx no se sabe si llegó a aplicarse"
    assert pendiente["routine_id"] == "r1"
    assert "NO se sabe" in (r.error or "")
    assert r.http_status == 500


def test_un_429_tambien_deja_la_marca_por_prudencia(tmp_path):
    """No se decide por el número, se decide por si se sabe o no."""
    c, _ = cliente(
        tmp_path, [FakeResponse(200, REMOTO), FakeResponse(503, text="mantenimiento")]
    )
    c.write_routine("r1", cuerpo_valido())
    assert read_pending(tmp_path) is not None


def test_un_cuerpo_que_no_pasa_el_contrato_no_llega_a_poner_la_marca(tmp_path):
    """El orden de los pasos, comprobado.

    Un cuerpo mal construido no sale del proceso, no toca la red y deja la
    rutina intacta por definición. Antes la marca se escribía ANTES de construir
    el cuerpo, así que ese caso -el más inofensivo de todos- dejaba puesto el
    aviso más grave que tiene el sistema.
    """
    c, doble = cliente(tmp_path, [FakeResponse(200, REMOTO)])
    malo = cuerpo_valido()
    malo["routine"]["notes"] = ["una lista donde va texto"]

    r = c.write_routine("r1", malo)

    assert not r.written
    assert read_pending(tmp_path) is None, (
        "el PUT ni se intentó: marcar «estado desconocido» aquí es gritar por "
        "el caso en el que más se sabe lo que hay"
    )
    assert "notas de la rutina" in (r.error or "")
    assert [l["verb"] for l in doble.llamadas] == ["get"], "no debió salir el PUT"
    assert r.backup is not None and r.backup.path.is_file()
    assert r.http_status is None, "no hubo respuesta: no hay código que guardar"


def test_el_codigo_http_viaja_en_el_resultado(tmp_path):
    """`hevy_writes.http_status` estuvo declarada y a NULL desde el primer día.

    Sin ella la auditoría no distingue «Hevy contestó 400» de «no se pudo ni
    preguntar», que es justo la diferencia que decide si hay que ir a mirar la
    rutina a mano o no.
    """
    c, _ = cliente(tmp_path, [FakeResponse(200, REMOTO), FakeResponse(200, {"ok": 1})])
    assert c.write_routine("r1", cuerpo_valido()).http_status == 200


def test_si_la_red_falla_la_marca_se_queda_puesta(tmp_path):
    """No sabemos si el PUT llegó. Fingir que no llegó sería mentir."""
    doble = FakeHTTP([FakeResponse(200, REMOTO)])
    c = HevyClient(api_key="k", data_root=tmp_path, write_enabled=True)

    llamadas = {"n": 0}

    def cliente_que_revienta():
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            return doble
        raise ConnectionError("se cayó la red a mitad")

    c._client = cliente_que_revienta  # type: ignore[method-assign]
    r = c.write_routine("r1", cuerpo_valido())

    assert not r.written
    assert "NO se sabe si Hevy" in (r.error or "")
    pendiente = read_pending(tmp_path)
    assert pendiente is not None, "sin marca, mañana nadie sabría que hay una rutina dudosa"
    assert pendiente["routine_id"] == "r1"
    assert Path(pendiente["backup"]).is_file()


def test_un_get_que_falla_impide_la_escritura(tmp_path):
    c, _ = cliente(tmp_path, [FakeResponse(500, text="boom")])
    with pytest.raises(HevyError, match="500"):
        c.write_routine("r1", cuerpo_valido())
    assert read_pending(tmp_path) is None


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restaurar_ignora_el_interruptor(tmp_path):
    """Si el modo seguro bloqueara la reversión, impediría deshacer un desastre
    causado mientras el interruptor estaba abierto."""
    save_backup(tmp_path, "r1", REMOTO)
    c, doble = cliente(tmp_path, [FakeResponse(200, {"ok": True})], write_enabled=False)
    r = c.restore("r1")
    assert r.written
    assert doble.llamadas[0]["verb"] == "put"
    enviado = doble.llamadas[0]["json"]["routine"]["exercises"]
    assert enviado == cuerpo_para_put(REMOTO)["routine"]["exercises"]


def test_restaurar_retira_la_marca_de_escritura_en_curso(tmp_path):
    save_backup(tmp_path, "r1", REMOTO)
    marca = pending_marker(tmp_path)
    marca.write_text(json.dumps({"routine_id": "r1"}), encoding="utf-8")
    c, _ = cliente(tmp_path, [FakeResponse(200, {})], write_enabled=False)
    c.restore("r1")
    assert read_pending(tmp_path) is None


def test_escribir_y_revertir_devuelve_la_rutina_a_como_estaba(tmp_path):
    """El viaje completo: se escribe encima, se revierte, vuelve lo de antes.

    Los otros tests prueban los dos eslabones por separado -que la escritura deja
    una copia igual al remoto, y que `restore` manda el contenido de una copia-.
    Ninguno prueba que encadenados devuelvan la rutina original, que es la única
    promesa que importa cuando algo ha salido mal a las siete de la mañana. Dos
    eslabones correctos con un cambio de forma entre medias -que la copia guarde
    el cuerpo del PUT en vez de la rutina remota, por ejemplo- daría los dos
    tests en verde y una reversión que no revierte.
    """
    c, doble = cliente(
        tmp_path,
        [
            FakeResponse(200, REMOTO),      # lectura previa a la escritura
            FakeResponse(200, {"ok": True}),  # el PUT que pisa la rutina
            FakeResponse(200, {"ok": True}),  # el PUT de la reversión
        ],
    )

    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    assert c.write_routine("r1", nuevo).written
    pisado = doble.llamadas[1]["json"]["routine"]
    assert pisado["exercises"][0]["sets"][1]["weight_kg"] == 60, "no se llegó a pisar"

    r = c.restore("r1")

    assert r.written
    devuelto = doble.llamadas[2]["json"]["routine"]
    assert devuelto["title"] == REMOTO["title"]
    assert devuelto["notes"] == REMOTO["notes"]
    # Se compara contra la copia PASADA POR EL CONTRATO, no contra la respuesta
    # cruda del GET. La diferencia no es una licencia para que el test pase: es
    # que `index` y `title` viajan en la respuesta y Hevy los RECHAZA en la
    # petición, así que el cuerpo correcto no PUEDE ser igual al remoto. Lo que
    # tiene que sobrevivir al viaje -qué ejercicios, en qué orden, cuántas
    # series y con qué peso- sobrevive, y es lo que se comprueba.
    assert devuelto["exercises"] == cuerpo_para_put(REMOTO)["routine"]["exercises"], (
        "lo revertido no es lo que había antes de escribir"
    )
    assert devuelto["exercises"][0]["sets"][1]["weight_kg"] == 55
    assert [e["exercise_template_id"] for e in devuelto["exercises"]] == [
        e["exercise_template_id"] for e in REMOTO["exercises"]
    ], "la reversión ha cambiado los ejercicios o su orden"


def test_sin_copia_no_se_puede_revertir(tmp_path):
    c, _ = cliente(tmp_path, [])
    with pytest.raises(HevyError, match="no hay ninguna copia"):
        c.restore("r1")


def test_una_reversion_rechazada_lanza(tmp_path):
    save_backup(tmp_path, "r1", REMOTO)
    c, _ = cliente(tmp_path, [FakeResponse(403, text="prohibido")])
    with pytest.raises(HevyError, match="403"):
        c.restore("r1")


# ---------------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------------


@dataclass
class SettingsFalsos:
    hevy_api_key: str = "clave"
    hevy_api_base: str = "https://api.hevyapp.com"
    database_url: str = "sqlite:///data/app.db"


def test_build_client_lee_el_interruptor_del_yaml(cfg):
    c = hevy.build_client(SettingsFalsos(), cfg)
    assert c.write_enabled is bool(
        cfg.raw["integrations"]["hevy"].get("write_enabled", False)
    )


def test_build_client_apagado_por_defecto_si_falta_la_seccion():
    c = hevy.build_client(SettingsFalsos(), {})
    assert c.write_enabled is False


def test_build_client_exige_la_clave_en_el_env():
    with pytest.raises(HevyError, match="HEVY_API_KEY"):
        hevy.build_client(SettingsFalsos(hevy_api_key=""), {})


def test_build_client_lee_cuantas_copias_se_guardan(cfg):
    c = hevy.build_client(SettingsFalsos(), cfg)
    assert c.backup_keep_last == cfg.raw["integrations"]["hevy"]["backup"]["keep_last"]


def test_sin_la_seccion_se_guardan_todas_como_siempre():
    """El defecto de un ajuste ausente tiene que ser lo que se hacía antes.
    Ponerse a borrar por iniciativa propia es justo lo contrario."""
    assert hevy.build_client(SettingsFalsos(), {}).backup_keep_last is None


# ---------------------------------------------------------------------------
# Cuántas copias se conservan
#
# `keep_last: 30` estaba escrito en el YAML desde el principio y no lo leía
# nadie. Una copia por escritura y una escritura al día: la carpeta crecía sin
# techo. Un disco que se llena despacio es la forma de quedarse sin disco que
# menos se ve venir, y lo primero que falla al llenarse es `save_backup`, que es
# exactamente lo que impide escribir en Hevy sin copia.
# ---------------------------------------------------------------------------


def _copias(tmp_path, cuantas: int, routine_id: str = "r1") -> list[Path]:
    """Copias con fecha distinta. `save_backup` las nombra por segundo, así que
    dos seguidas en el mismo segundo se pisarían y no habría nada que podar."""
    carpeta = tmp_path / "hevy_backups" / routine_id
    carpeta.mkdir(parents=True, exist_ok=True)
    hechas = []
    for i in range(cuantas):
        f = carpeta / f"2026090{i}-120000.json"
        f.write_text(json.dumps({
            "routine_id": routine_id,
            "taken_at": f"2026-09-0{i}T12:00:00",
            "routine": {**REMOTO, "title": f"copia {i}"},
        }), encoding="utf-8")
        hechas.append(f)
    return hechas


def test_se_borran_las_mas_viejas_y_se_quedan_las_ultimas(tmp_path):
    hechas = _copias(tmp_path, 5)

    borradas = hevy.prune_backups(tmp_path, "r1", keep_last=3)

    assert borradas == hechas[:2]
    quedan = sorted((tmp_path / "hevy_backups" / "r1").glob("*.json"))
    assert quedan == hechas[2:]


def test_se_conserva_siempre_la_mas_reciente(tmp_path):
    """Lo único innegociable: después de podar tiene que poder revertirse."""
    _copias(tmp_path, 5)

    hevy.prune_backups(tmp_path, "r1", keep_last=1)

    ultima = latest_backup(tmp_path, "r1")
    assert ultima is not None
    assert ultima.path.name == "20260904-120000.json"


def test_con_menos_copias_que_el_limite_no_se_borra_nada(tmp_path):
    hechas = _copias(tmp_path, 3)
    assert hevy.prune_backups(tmp_path, "r1", keep_last=30) == []
    assert all(f.is_file() for f in hechas)


def test_un_limite_de_cero_no_se_obedece(tmp_path, caplog):
    """`keep_last: 0` no es un límite, es la orden de quedarse sin ninguna
    copia. El validador ya lo rechaza; aquí se comprueba que aunque llegara
    -por código, no por YAML- este módulo no se deja.

    Se exige además el AVISO, y no por gusto: `ficheros[:-0]` es la lista vacía,
    así que con un cero no se borraría nada aunque no hubiera guardia ninguna. Es
    decir, que lo correcto pasaría por accidente. Comprobar solo los ficheros
    daba por buena una versión sin la guardia, que es la que un día se encuentra
    con un -1 y sí borra.
    """
    hechas = _copias(tmp_path, 3)

    with caplog.at_level(logging.WARNING, logger="app.integrations.hevy"):
        assert hevy.prune_backups(tmp_path, "r1", keep_last=0) == []

    assert all(f.is_file() for f in hechas)
    assert "keep_last=0" in caplog.text


def test_un_limite_negativo_tampoco(tmp_path):
    hechas = _copias(tmp_path, 3)
    assert hevy.prune_backups(tmp_path, "r1", keep_last=-1) == []
    assert all(f.is_file() for f in hechas)


def test_sin_carpeta_no_revienta(tmp_path):
    assert hevy.prune_backups(tmp_path, "nunca_escrita", keep_last=5) == []


def test_una_copia_que_no_se_deja_borrar_no_interrumpe_nada(tmp_path, monkeypatch):
    """Una copia vieja que se queda es un problema de disco. Abortar por eso
    convertiría un problema de limpieza en un día sin entrenamiento."""
    _copias(tmp_path, 4)

    def no_se_puede(self):  # noqa: ANN001
        raise OSError("en uso")

    monkeypatch.setattr(Path, "unlink", no_se_puede)
    assert hevy.prune_backups(tmp_path, "r1", keep_last=1) == []


def test_se_poda_DESPUES_de_guardar_la_copia_nueva(tmp_path):
    """El orden es lo único que garantiza que no hay un instante sin copia
    buena. Con `keep_last=1` y una escritura, la que sobrevive tiene que ser la
    que se acaba de tomar, no una de las viejas."""
    _copias(tmp_path, 3)
    c, _ = cliente(tmp_path, [FakeResponse(200, REMOTO), FakeResponse(200, {})],
                   write_enabled=True)
    c.backup_keep_last = 1

    r = c.write_routine("r1", cuerpo_valido())

    assert r.written
    quedan = sorted((tmp_path / "hevy_backups" / "r1").glob("*.json"))
    assert len(quedan) == 1
    assert json.loads(quedan[0].read_text(encoding="utf-8"))["routine"] == REMOTO


def test_sin_limite_las_copias_se_acumulan(tmp_path):
    """El comportamiento de siempre, escrito para que no se pierda por
    descuido al tocar la poda."""
    _copias(tmp_path, 3)
    c, _ = cliente(tmp_path, [FakeResponse(200, REMOTO), FakeResponse(200, {})],
                   write_enabled=True)
    assert c.backup_keep_last is None

    c.write_routine("r1", cuerpo_valido())

    assert len(list((tmp_path / "hevy_backups" / "r1").glob("*.json"))) == 4


# ---------------------------------------------------------------------------
# El contrato del cuerpo del PUT
# ---------------------------------------------------------------------------


def test_el_cuerpo_del_put_no_lleva_las_claves_que_hevy_rechaza():
    """La forma del GET no es la forma del PUT, y confundirlas es un 400 entero.

    Este test es la cicatriz de un fallo real. Hasta el 2026-09-12 el sistema no
    había hecho NUNCA un PUT que llegara a Hevy -`write_enabled` arrancó en
    `false` y nadie lo abrió-, así que el cuerpo nunca se validó contra la API
    de verdad. `restore` reenviaba la copia tal cual, con `index` y `title`, y
    Hevy contestaba `400 Unrecognized key(s) in object: 'index'`. Seis tests en
    verde y la reversión rota.
    """
    limpio = cuerpo_para_put(REMOTO)["routine"]
    for ex in limpio["exercises"]:
        assert "index" not in ex
        assert "title" not in ex
        for s in ex["sets"]:
            assert "index" not in s


def test_el_cuerpo_del_put_conserva_lo_que_importa():
    """Limpiar no puede convertirse en perder.

    Un saneado demasiado entusiasta que se llevara por delante el
    `superset_id` deshace las superseries en la app, y eso no da ningún error:
    la rutina queda escrita, distinta, y en silencio.
    """
    remoto = {
        "title": "Día 1",
        "notes": "una nota",
        "exercises": [
            {
                "index": 0,
                "title": "Hip thrust",
                "exercise_template_id": "AAAA1111",
                "superset_id": 7,
                "rest_seconds": 90,
                "notes": "cuidado lumbar",
                "sets": [
                    {"index": 0, "type": "warmup", "reps": 10, "weight_kg": 20.0,
                     "distance_meters": None, "duration_seconds": None,
                     "custom_metric": None},
                ],
            }
        ],
    }
    r = cuerpo_para_put(remoto)["routine"]
    assert r["title"] == "Día 1" and r["notes"] == "una nota"
    ex = r["exercises"][0]
    assert ex["exercise_template_id"] == "AAAA1111"
    assert ex["superset_id"] == 7, "la supersería se deshace en la app y sin avisar"
    assert ex["rest_seconds"] == 90
    assert ex["notes"] == "cuidado lumbar"
    assert ex["sets"][0] == {
        "type": "warmup", "reps": 10, "weight_kg": 20.0,
        "distance_meters": None, "duration_seconds": None, "custom_metric": None,
    }


def test_una_clave_desconocida_de_hevy_es_error_y_no_un_descarte_callado():
    """Si la API añade un campo, hay que decidir qué se hace con él.

    Tragárselo escribiría en Hevy una rutina a la que le falta algo -un RPE, una
    nota nueva- sin que nadie se entere. Que reviente es recuperable: la copia
    ya está hecha y el PUT todavía no ha salido.
    """
    remoto = {
        "title": "x", "notes": None,
        "exercises": [{"exercise_template_id": "A", "rpe_objetivo": 8, "sets": []}],
    }
    with pytest.raises(HevyError, match="rpe_objetivo"):
        cuerpo_para_put(remoto)

    con_serie_rara = {
        "title": "x", "notes": None,
        "exercises": [{"exercise_template_id": "A", "sets": [{"type": "normal", "tempo": "3011"}]}],
    }
    with pytest.raises(HevyError, match="tempo"):
        cuerpo_para_put(con_serie_rara)


def test_cuerpo_para_put_acepta_las_dos_formas_de_entrada():
    """Envuelto o pelado: del motor llega envuelto y de una copia llega pelado."""
    pelado = cuerpo_para_put(REMOTO)
    envuelto = cuerpo_para_put({"routine": REMOTO})
    assert pelado == envuelto


def test_el_payload_del_motor_pasa_el_contrato():
    """Lo que construye el motor cada mañana tiene que poder escribirse.

    Sin esto, el contrato se comprobaría solo sobre las copias y no sobre la
    escritura del día, que es la que ocurre 250 veces al año.
    """
    nuevo = build_routine_payload(SesionFalsa(exercises=[ejercicio()]), {})
    limpio = cuerpo_para_put(nuevo)["routine"]
    assert limpio["exercises"][0]["exercise_template_id"] == "AAAA1111"
    assert len(limpio["exercises"][0]["sets"]) == 2
    for ex in limpio["exercises"]:
        assert not ({"index", "title"} & set(ex))
