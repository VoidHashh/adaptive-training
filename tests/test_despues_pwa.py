"""La pantalla de después de entrenar, pulsada de verdad.

POR QUÉ ESTE FICHERO Y NO UNA BÚSQUEDA DE TEXTO EN `despues.js`
--------------------------------------------------------------
Lo que hay que demostrar aquí son conversiones que no dejan rastro. Un
deslizador sin tocar tiene `value="5"` porque ahí se dibuja el pulgar; leerlo
y mandarlo produce un formulario perfectamente válido con un cinco que nadie
contestó. Un botón de elección pulsado dos veces tiene que volver a «sin
contestar», y si no vuelve, un toque por error se queda guardado como
respuesta. Ninguna de las dos cosas da error ni se ve leyendo el archivo: se
ve pulsando y mirando el JSON que sale por el cable. Eso hace
`tests/despues_pwa.mjs`, con el mismo patrón que el del check-in.

EL PAYLOAD SE CONSTRUYE CON EL VOCABULARIO DE VERDAD
----------------------------------------------------
`_sesion()` no lleva las opciones escritas: las saca de `app/engine/feedback.py`,
igual que el endpoint. Escritas aquí, el día que cambiara una opción en Python
estos tests seguirían pulsando la vieja y pasando en verde, que es exactamente
el tipo de verde que este repositorio persigue.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from app.engine.feedback import (
    ELECCIONES,
    FALTA,
    HECHO,
    NINGUNO_EN_ESPECIAL,
    PREGUNTA_FALTA,
    PREGUNTA_MAS_COSTOSO,
    RESPUESTAS_FALTA,
    RESPUESTAS_HECHO,
    SIN_PROBLEMA,
    escalas_con_manana,
)
from app.models import Checkin
from app.settings import REPO_ROOT
from tests.dobles import doble_de

RAIZ = REPO_ROOT

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="hace falta `node` para pulsar la pantalla"
)


@doble_de(Checkin)
class _Manana:
    """El check-in de esa mañana, con lo único que las escalas leen de él.

    Doble de la fila de `checkins`, y declarado como tal: `escalas_con_manana`
    le hace `getattr` a los mismos dos campos que tendría una fila de verdad, y
    si un día esos campos cambian de nombre en el modelo, `doble_de` lo canta.
    """

    def __init__(self, lower_discomfort=None, training_desire=None):
        self.lower_discomfort = lower_discomfort
        self.training_desire = training_desire


def _sesion(*, ejercicios=None, manana=None, guardado=None, **cambios) -> dict:
    """Un `/api/sesion/hoy` con la forma y el vocabulario del endpoint real."""
    if ejercicios is None:
        ejercicios = [
            {"key": "peso_muerto", "name": "Peso muerto rumano", "estado": HECHO, "respuesta": None},
            {"key": "extension_espalda", "name": "Extensión de espalda", "estado": FALTA, "respuesta": None},
            {"key": "plancha", "name": "Plancha", "estado": HECHO, "respuesta": None},
        ]
    base = {
        "day": "2026-09-25",
        "hay_sesion": True,
        "entrenamientos": 1,
        "ejercicios": ejercicios,
        "respuestas": {HECHO: RESPUESTAS_HECHO, FALTA: RESPUESTAS_FALTA},
        "elecciones": list(ELECCIONES),
        "escalas": escalas_con_manana(manana),
        "textos": {
            "mas_costoso": PREGUNTA_MAS_COSTOSO,
            "falta": PREGUNTA_FALTA,
            "sin_problema": SIN_PROBLEMA,
            "ninguno": NINGUNO_EN_ESPECIAL,
        },
        "guardado": {
            "enviado": False, "rpe": None, "lower_discomfort_after": None,
            "training_desire_after": None, "satisfaccion": None,
            "cantidad": None, "tecnica": None, "mas_costoso": None, "nota": None,
            **(guardado or {}),
        },
        "lumbar_manana": getattr(manana, "lower_discomfort", None),
        "motivo_sin_hevy": None,
    }
    base.update(cambios)
    return base


def _pulsar(tmp_path, hoy: dict | None, acciones: list[dict], *, post=None,
            sin_red=False, status_hoy=200) -> dict:
    guion = tmp_path / "guion.json"
    guion.write_text(json.dumps({
        "hoy": {"status": status_hoy, "json": hoy},
        "post": post or {},
        "sin_red": sin_red,
        "acciones": acciones,
    }, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(
        ["node", "tests/despues_pwa.mjs", str(guion)],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert r.returncode == 0, f"el arnés no terminó:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# Lo que no se contesta no se envía
# ---------------------------------------------------------------------------


def test_una_escala_sin_tocar_no_viaja_y_una_tocada_si(tmp_path):
    """La pareja que justifica este fichero entero."""
    out = _pulsar(tmp_path, _sesion(), [
        {"cargar": True},
        {"escala": "rpe", "valor": 7},
        {"guardar": True},
    ])
    c = out["cuerpo"]
    assert c["rpe"] == 7
    for sin_tocar in ("lower_discomfort_after", "training_desire_after", "satisfaccion"):
        assert sin_tocar not in c, (
            f"`{sin_tocar}` no se ha tocado y ha viajado como {c.get(sin_tocar)!r}: "
            f"es el `value=\"5\"` del pulgar convertido en una respuesta"
        )


def test_un_cero_tocado_SI_viaja(tmp_path):
    """El caso de al lado del anterior, y el que un `if (valor)` rompería.

    Cero es una respuesta -«nada de molestias», «nada de esfuerzo»- y es la más
    fácil de perder: es falso en JavaScript. Un filtro escrito con prisa para no
    mandar lo que no se ha tocado se llevaría también los ceros.
    """
    out = _pulsar(tmp_path, _sesion(), [
        {"cargar": True},
        {"escala": "lower_discomfort_after", "valor": 0},
        {"guardar": True},
    ])
    assert out["cuerpo"]["lower_discomfort_after"] == 0


def test_una_eleccion_pulsada_dos_veces_vuelve_a_sin_contestar(tmp_path):
    """Sin esto, un toque por error se quedaría guardado como respuesta y no
    habría forma de deshacerlo desde la pantalla."""
    out = _pulsar(tmp_path, _sesion(), [
        {"cargar": True},
        {"eleccion": "cantidad", "valor": "corta"},
        {"eleccion": "cantidad", "valor": "corta"},
        {"eleccion": "tecnica", "valor": "se_iba"},
        {"guardar": True},
    ])
    assert "cantidad" not in out["cuerpo"]
    assert out["cuerpo"]["tecnica"] == "se_iba"


def test_cambiar_de_opcion_se_queda_con_la_ultima(tmp_path):
    out = _pulsar(tmp_path, _sesion(), [
        {"cargar": True},
        {"eleccion": "cantidad", "valor": "corta"},
        {"eleccion": "cantidad", "valor": "larga"},
        {"guardar": True},
    ])
    assert out["cuerpo"]["cantidad"] == "larga"


# ---------------------------------------------------------------------------
# Los ejercicios
# ---------------------------------------------------------------------------


def test_lo_que_falta_va_arriba_y_lo_hecho_plegado(tmp_path):
    """La inversión que quita densidad: una sesión sin incidencias se contesta
    sin abrir ni una lista."""
    out = _pulsar(tmp_path, _sesion(), [{"cargar": True}])
    assert out["faltan"] == ["Extensión de espalda"]
    assert out["hechos_titulo"].startswith("Los otros 2, bien")
    assert out["hechos_abierto"] is False


def test_lo_hecho_se_abre_solo_si_ya_habia_algo_marcado(tmp_path):
    """Plegar un problema ya contado lo escondería al volver a abrir."""
    ejs = [
        {"key": "peso_muerto", "name": "Peso muerto", "estado": HECHO,
         "respuesta": "molestia_lumbar"},
        {"key": "plancha", "name": "Plancha", "estado": HECHO, "respuesta": None},
    ]
    out = _pulsar(tmp_path, _sesion(ejercicios=ejs), [{"cargar": True}])
    assert out["hechos_abierto"] is True


def test_la_respuesta_de_cada_ejercicio_llega_al_servidor(tmp_path):
    out = _pulsar(tmp_path, _sesion(), [
        {"cargar": True},
        {"respuesta": "extension_espalda", "valor": "molestia_lumbar"},
        {"respuesta": "peso_muerto", "valor": "peso_alto"},
        {"guardar": True},
    ])
    por_clave = {e["key"]: e for e in out["cuerpo"]["ejercicios"]}
    assert por_clave["extension_espalda"]["respuesta"] == "molestia_lumbar"
    assert por_clave["extension_espalda"]["estado"] == FALTA
    assert por_clave["peso_muerto"]["respuesta"] == "peso_alto"
    # Y lo que no se tocó viaja como `null`, no desaparece: el servidor tiene
    # que saber que el ejercicio estaba en la sesión aunque no dijera nada.
    assert por_clave["plancha"]["respuesta"] is None


def test_volver_a_sin_problema_quita_la_respuesta(tmp_path):
    out = _pulsar(tmp_path, _sesion(), [
        {"cargar": True},
        {"respuesta": "peso_muerto", "valor": "peso_alto"},
        {"respuesta": "peso_muerto", "valor": ""},
        {"guardar": True},
    ])
    por_clave = {e["key"]: e for e in out["cuerpo"]["ejercicios"]}
    assert por_clave["peso_muerto"]["respuesta"] is None


def test_el_que_mas_costo_viaja_y_ninguno_no(tmp_path):
    con = _pulsar(tmp_path, _sesion(), [
        {"cargar": True}, {"costoso": "peso_muerto"}, {"guardar": True},
    ])
    assert con["cuerpo"]["mas_costoso"] == "peso_muerto"

    sin = _pulsar(tmp_path, _sesion(), [
        {"cargar": True}, {"costoso": "peso_muerto"}, {"costoso": ""}, {"guardar": True},
    ])
    assert "mas_costoso" not in sin["cuerpo"]


# ---------------------------------------------------------------------------
# La pareja de la mañana
# ---------------------------------------------------------------------------


def test_la_escala_con_pareja_ensena_el_valor_de_esta_manana(tmp_path):
    out = _pulsar(
        tmp_path, _sesion(manana=_Manana(lower_discomfort=1, training_desire=9)),
        [{"cargar": True}],
    )
    por_clave = {e["key"]: e for e in out["escalas"]}
    assert por_clave["lower_discomfort_after"]["manana"] == "esta mañana: 1"
    assert por_clave["training_desire_after"]["manana"] == "esta mañana: 9"
    assert por_clave["rpe"]["manana"] is None


def test_sin_check_in_esta_manana_no_se_inventa_un_punto_de_partida(tmp_path):
    """Un «esta mañana: 5» que nadie contestó convertiría la comparación en una
    afirmación falsa. Sin dato, no se enseña nada."""
    out = _pulsar(tmp_path, _sesion(manana=None), [{"cargar": True}])
    assert all(e["manana"] is None for e in out["escalas"])


def test_un_cero_de_esta_manana_se_ensena(tmp_path):
    """El cero otra vez: «sin molestias esta mañana» es un dato, y un `if` mal
    puesto lo trataría como si no hubiera check-in."""
    out = _pulsar(tmp_path, _sesion(manana=_Manana(lower_discomfort=0)), [{"cargar": True}])
    por_clave = {e["key"]: e for e in out["escalas"]}
    assert por_clave["lower_discomfort_after"]["manana"] == "esta mañana: 0"


# ---------------------------------------------------------------------------
# Rectificar
# ---------------------------------------------------------------------------


def test_lo_ya_guardado_se_precarga_y_se_vuelve_a_mandar(tmp_path):
    """Volver a abrir para añadir una cosa no puede obligar a contestar las demás,
    ni perderlas si no se tocan."""
    out = _pulsar(
        tmp_path,
        _sesion(guardado={"enviado": True, "rpe": 8, "cantidad": "justa"}),
        [{"cargar": True}, {"escala": "satisfaccion", "valor": 6}, {"guardar": True}],
    )
    c = out["cuerpo"]
    assert c["rpe"] == 8
    assert c["cantidad"] == "justa"
    assert c["satisfaccion"] == 6


# ---------------------------------------------------------------------------
# Lo que puede salir mal
# ---------------------------------------------------------------------------


def test_sin_entreno_en_hevy_lo_dice_y_no_ensena_el_formulario(tmp_path):
    out = _pulsar(tmp_path, _sesion(hay_sesion=False, ejercicios=[]), [{"cargar": True}])
    assert out["form_visible"] is False
    assert "Todavía no hay ningún entreno" in out["aviso"]
    assert out["veces_enviado"] == 0


def test_si_hevy_no_contesta_se_dice_por_que_y_no_que_no_hay_entreno(tmp_path):
    """«No hay entreno» y «no se ha podido preguntar» piden cosas opuestas -esperar
    o arreglar las claves- y en una línea se leen igual."""
    out = _pulsar(
        tmp_path,
        _sesion(hay_sesion=False, ejercicios=[], motivo_sin_hevy="falta HEVY_API_KEY"),
        [{"cargar": True}],
    )
    assert "falta HEVY_API_KEY" in out["aviso"]
    assert "Todavía no hay" not in out["aviso"]


def test_sin_red_no_se_pinta_nada_a_medias(tmp_path):
    out = _pulsar(tmp_path, _sesion(), [{"cargar": True}], sin_red=True)
    assert out["form_visible"] is False
    assert "Sin conexión" in out["aviso"]


def test_un_rechazo_del_servidor_se_ensena_con_su_motivo(tmp_path):
    """El 422 trae escrito por el validador qué opción ha dejado de existir, y es
    la única pista para saberlo desde el móvil."""
    out = _pulsar(
        tmp_path, _sesion(),
        [{"cargar": True}, {"guardar": True}],
        post={"status": 422, "json": {"detail": "cantidad: 'regular' no es una respuesta válida"}},
    )
    assert out["guardado"] is False
    assert "'regular' no es una respuesta válida" in out["aviso"]


# ---------------------------------------------------------------------------
# La cabecera de `despues.js` dice que no lleva preguntas escritas
# ---------------------------------------------------------------------------


def test_despues_js_no_lleva_ni_una_pregunta_escrita():
    """La afirmación de su cabecera, atada a donde se puede romper.

    La primera versión del fichero la hacía y la incumplía a la vez: los
    enunciados de las elecciones estaban escritos en el código de debajo. Se vio
    abriendo la pantalla en el navegador, no en ningún test.

    Se buscan los signos de apertura de pregunta en el código SIN comentarios,
    porque los comentarios los citan entre comillas para explicar precisamente
    esto. Un «¿» en el código es una pregunta que la pantalla escribe en vez de
    recibirla del servidor.
    """
    codigo = (RAIZ / "static" / "despues.js").read_text(encoding="utf-8")
    sin_comentarios = re.sub(r"/\*.*?\*/", "", codigo, flags=re.S)
    sin_comentarios = re.sub(r"//[^\n]*", "", sin_comentarios)
    preguntas = [l.strip() for l in sin_comentarios.splitlines() if "¿" in l]
    assert not preguntas, (
        f"`despues.js` escribe preguntas en vez de recibirlas del servidor: "
        f"{preguntas}. Van en `app/engine/feedback.py`"
    )
