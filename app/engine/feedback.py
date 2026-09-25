"""Qué se puede contestar del entreno de hoy, y qué significa cada respuesta.

POR QUÉ ES UN MÓDULO Y NO UNAS CONSTANTES EN LA API
---------------------------------------------------
Porque las mismas listas hacen falta en tres sitios: la pantalla que las pinta,
el endpoint que valida lo que llega, y el análisis que cuenta cuántas veces un
ejercicio ha dado guerra. Escritas tres veces, se separan; y la forma de que se
separen sin que nada falle es que la pantalla ofrezca una opción que el
validador rechaza, o que el contador busque una cadena que ya nadie envía.

Por eso las ETIQUETAS también están aquí y las sirve la API a la pantalla, en
vez de repetirlas en `static/`. Una lista de opciones en JavaScript y otra en
Python es el mismo defecto que este repositorio persigue: dos piezas que
empiezan iguales y dejan de serlo sin ruido.

LOS DOS VOCABULARIOS, Y POR QUÉ NO SON UNO
-------------------------------------------
Un ejercicio que SE HIZO y otro que NO APARECE no admiten las mismas respuestas.
«Lo hice y no lo apunté» no tiene sentido para algo que está apuntado, y «no
pude con el peso» no lo tiene para algo que no se llegó a tocar. Fundirlos en
una sola lista obligaría a ofrecer opciones imposibles y a validar a mano cuáles
valen en cada caso, que es la manera de que un día valga cualquiera.

Tres respuestas SÍ están en los dos -la espalda, otra molestia, la máquina-, y
eso es deliberado: son las que se cuentan por ejercicio a lo largo de los meses,
y si estuvieran escritas distinto en cada lado el contador tendría que saberlo.
"""

from __future__ import annotations

from typing import Any

from app.engine.sets import warmup_flags

HECHO = "hecho"
FALTA = "falta"
ESTADOS = (HECHO, FALTA)

# Lo que se puede contestar de un ejercicio QUE APARECE en el entrenamiento.
# `None` -no contestar- es el caso normal y significa "sin problema": una
# sesión buena se envía sin tocar ni uno de estos desplegables.
RESPUESTAS_HECHO: dict[str, str] = {
    "molestia_lumbar": "me molestó la espalda",
    "molestia_otra": "me molestó otra cosa",
    "peso_alto": "no pude con el peso",
    "maquina": "problema con la máquina",
}

# Lo que se puede contestar de un ejercicio DEL PLAN QUE NO APARECE. Aquí no hay
# opción vacía a propósito: si el ejercicio falta, pasó algo, y «no lo sé» ya lo
# dice el sistema solo cuando no se contesta el formulario.
RESPUESTAS_FALTA: dict[str, str] = {
    "hecho_sin_apuntar": "lo hice y no lo apunté",
    "molestia_lumbar": "lo dejé por la espalda",
    "molestia_otra": "lo dejé por otra molestia",
    "peso_alto": "no pude con el peso",
    "sin_tiempo": "no me dio tiempo",
    "maquina": "la máquina estaba ocupada o rota",
    "saltado": "me lo salté",
}

# Las respuestas que significan «algo me dolió». Son las que se cuentan por
# ejercicio a lo largo de los meses, y la lumbar va aparte de la otra porque
# con una hernia L4-L5 no son la misma noticia.
MOLESTIAS = ("molestia_lumbar", "molestia_otra")

# La única respuesta que cambia lo que el motor hace, y no solo lo que cuenta.
# Un ejercicio que se hizo y no se apuntó NO es un ejercicio incumplido: la
# reconciliación lo estaba contando como fallo y rompiendo la racha por un
# olvido de registro. Ver `app/runner.py`.
HECHO_SIN_APUNTAR = "hecho_sin_apuntar"


class FeedbackError(ValueError):
    """Ha llegado una respuesta que no está en el vocabulario.

    Revienta en vez de guardar. Una cadena que nadie reconoce se guardaría
    igual de bien y saldría a cero en todos los contadores para siempre, sin que
    nada lo dijera: el ejercicio que más guerra da aparecería con cero avisos.
    """


def respuestas_de(estado: str) -> dict[str, str]:
    if estado not in ESTADOS:
        raise FeedbackError(
            f"estado desconocido {estado!r}. Válidos: {list(ESTADOS)}. Lo pone "
            f"el servidor al montar el formulario, así que un valor raro aquí "
            f"es que la pantalla y la API han dejado de hablar el mismo idioma"
        )
    return RESPUESTAS_HECHO if estado == HECHO else RESPUESTAS_FALTA


def validar_ejercicios(entradas: Any) -> list[dict[str, Any]]:
    """Comprueba la lista que llega del formulario y la devuelve limpia.

    No inventa nada y no descarta nada en silencio: lo que no encaja revienta.
    Un `respuesta` fuera de vocabulario que se guardara tal cual sería invisible
    -ningún contador lo busca- y a la vez estaría ocupando el sitio de la
    respuesta buena.
    """
    if entradas is None:
        return []
    if not isinstance(entradas, list):
        raise FeedbackError(
            f"los ejercicios tienen que venir en una lista y ha llegado "
            f"{type(entradas).__name__}"
        )

    limpio: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for i, e in enumerate(entradas, start=1):
        if not isinstance(e, dict):
            raise FeedbackError(f"el ejercicio {i} no es un objeto: {e!r}")
        key = str(e.get("key") or "").strip()
        if not key:
            raise FeedbackError(f"el ejercicio {i} viene sin `key`")
        if key in vistos:
            raise FeedbackError(
                f"el ejercicio {key!r} aparece dos veces. Guardarlo así dejaría "
                f"dos respuestas para lo mismo y el contador elegiría una sin "
                f"criterio"
            )
        vistos.add(key)

        estado = str(e.get("estado") or "")
        permitidas = respuestas_de(estado)

        respuesta = e.get("respuesta")
        if respuesta is not None:
            respuesta = str(respuesta)
            if respuesta not in permitidas:
                raise FeedbackError(
                    f"{key}: respuesta {respuesta!r} no vale para un ejercicio "
                    f"'{estado}'. Válidas: {sorted(permitidas)}"
                )
        elif estado == FALTA:
            # Un ejercicio que falta y no se contesta se queda sin contestar, y
            # eso es legítimo: no se puede obligar a explicarlo todo. Pero se
            # guarda como `None` explícito y no se cuela como "saltado", que es
            # una afirmación que nadie ha hecho.
            respuesta = None

        limpio.append({
            "key": key,
            "name": str(e.get("name") or key),
            "estado": estado,
            "respuesta": respuesta,
        })
    return limpio


def sin_apuntar(ejercicios: list[dict[str, Any]] | None) -> set[str]:
    """Los ejercicios que se hicieron aunque no consten en Hevy.

    Es lo único de este módulo que el motor consulta, y por eso va aquí y no en
    una comprensión suelta dentro de `runner`: quien lea `HECHO_SIN_APUNTAR`
    tiene que encontrar en el mismo sitio qué significa y quién lo usa.
    """
    return {
        e["key"]
        for e in (ejercicios or [])
        if e.get("respuesta") == HECHO_SIN_APUNTAR
    }


def molestaron(ejercicios: list[dict[str, Any]] | None) -> dict[str, str]:
    """`{ejercicio: respuesta}` de los que dieron una molestia, del tipo que sea.

    Se devuelve la respuesta y no un booleano porque la lumbar y «otra cosa» no
    son la misma noticia en una espalda con hernia, y un booleano las fundiría.
    """
    return {
        e["key"]: e["respuesta"]
        for e in (ejercicios or [])
        if e.get("respuesta") in MOLESTIAS
    }


# ---------------------------------------------------------------------------
# El cruce: qué del plan aparece en Hevy y qué no
# ---------------------------------------------------------------------------


def cruzar(
    plan: list[dict[str, Any]] | None,
    workouts: list[dict[str, Any]] | None,
    set_cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Los ejercicios del día, cada uno marcado `hecho` o `falta`.

    Devuelve primero los del PLAN -en el orden del plan, que es el orden en que
    se entrenan- y después los que se hicieron SIN estar planificados, que
    también forman parte de la sesión y también pueden haber dado guerra.

    UN EJERCICIO CON SOLO CALENTAMIENTO CUENTA COMO QUE FALTA
    ---------------------------------------------------------
    «Se abrió y se dejó» es una de las cuatro situaciones que este formulario
    existe para separar, y es la que más se parece a no haberlo hecho: no hay
    ni una serie efectiva. Marcarlo `hecho` porque la entrada existe en Hevy
    dejaría al usuario sin el desplegable que explica por qué lo dejó, que es
    justo el que hace falta ahí.

    LOS SUELTOS SE NOMBRAN CON EL `title` DE HEVY
    ---------------------------------------------
    Un ejercicio que no está en el plan no tiene `key` nuestra, así que se le
    pone `hevy:<template_id>` y el nombre que Hevy le da. No es un apaño: es el
    único nombre que existe para algo que este sistema no ha planificado, y
    dejarlos fuera por no tener clave propia perdería exactamente los entrenos
    que nadie más mira.
    """
    hechos: dict[str, dict[str, Any]] = {}
    for w in workouts or []:
        for ej in (w.get("exercises") or []):
            tid = str(ej.get("exercise_template_id") or "")
            if not tid:
                continue
            series = list(ej.get("sets") or [])
            flags = warmup_flags(series, set_cfg or {})
            efectivas = sum(1 for s, cal in zip(series, flags) if not cal)
            previo = hechos.get(tid)
            hechos[tid] = {
                "title": ej.get("title") or (previo or {}).get("title") or tid,
                # Una sesión partida en dos ratos suma las series de los dos:
                # media docena en un rato y media en otro es el ejercicio hecho.
                "efectivas": (previo or {}).get("efectivas", 0) + efectivas,
            }

    salida: list[dict[str, Any]] = []
    del_plan: set[str] = set()
    for ex in plan or []:
        tid = str(ex.get("template_id") or "")
        del_plan.add(tid)
        visto = hechos.get(tid)
        salida.append({
            "key": str(ex.get("key") or tid),
            "name": str(ex.get("name") or ex.get("key") or tid),
            "estado": HECHO if (visto and visto["efectivas"] > 0) else FALTA,
            "respuesta": None,
        })

    for tid, visto in hechos.items():
        if tid in del_plan or visto["efectivas"] <= 0:
            continue
        salida.append({
            "key": f"hevy:{tid}",
            "name": visto["title"],
            "estado": HECHO,
            "respuesta": None,
        })
    return salida


# ---------------------------------------------------------------------------
# Las dos elecciones sobre la sesión entera
# ---------------------------------------------------------------------------
#
# Van aquí y no en la API por lo mismo que los otros dos vocabularios: las pinta
# la pantalla, las valida el endpoint y las cuenta el análisis, y escritas tres
# veces se separan.

CANTIDAD: dict[str, str] = {
    "corta": "se me quedó corta",
    "justa": "justa",
    "larga": "se me hizo larga",
}

TECNICA: dict[str, str] = {
    "bien": "aguantó bien",
    "se_iba": "se me iba al final",
    "mal": "no aguantó",
}


def validar_eleccion(valor: Any, tabla: dict[str, str], campo: str) -> str | None:
    """`None` es no contestar, y es legítimo. Cualquier otra cosa, o vale o revienta.

    No se acepta una cadena vacía como «no contestada»: `""` y `None` llegarían
    por caminos distintos -un desplegable sin tocar y un campo ausente- y
    tratarlos igual es la forma de que un día se guarde `""` en la columna y
    ningún contador lo vea ni como respuesta ni como hueco.
    """
    if valor is None:
        return None
    valor = str(valor)
    if valor not in tabla:
        raise FeedbackError(
            f"{campo}: {valor!r} no es una respuesta válida. "
            f"Válidas: {sorted(tabla)}, o no contestar"
        )
    return valor


def validar_mas_costoso(valor: Any, ejercicios: list[dict[str, Any]]) -> str | None:
    """El que más costó tiene que ser UNO DE LOS DE HOY.

    Sin esta comprobación se guardaría cualquier cadena, y el cruce con el peso
    apuntado -que es para lo que existe la pregunta- buscaría un ejercicio que
    esa sesión no tuvo y saldría vacío para siempre sin que nada fallara.
    """
    if valor is None:
        return None
    valor = str(valor)
    claves = {e["key"] for e in ejercicios}
    if valor not in claves:
        raise FeedbackError(
            f"mas_costoso: {valor!r} no está entre los ejercicios de la sesión "
            f"({sorted(claves)})"
        )
    return valor


# ---------------------------------------------------------------------------
# Las cuatro escalas, con su pareja de la mañana
# ---------------------------------------------------------------------------
#
# Viajan a la pantalla desde aquí, igual que los deslizadores del check-in salen
# de `checkin_sliders`: la pantalla no lleva ni una etiqueta escrita. Si las
# llevara, cambiar un texto aquí no cambiaría lo que se lee en el móvil.
#
# `pareja` es la columna del check-in de esa mañana con la que se compara. La
# pantalla la enseña al lado -«esta mañana: 1»- porque el número de después
# solo significa algo restado del de antes, y sin verlo el usuario contesta en
# el vacío.

ESCALAS: tuple[dict[str, Any], ...] = (
    {"key": "rpe", "label": "Esfuerzo",
     "hint_low": "suave", "hint_high": "al límite"},
    {"key": "lower_discomfort_after", "label": "La espalda, ahora",
     "hint_low": "sin molestias", "hint_high": "mucho dolor",
     "pareja": "lower_discomfort"},
    {"key": "training_desire_after", "label": "Ganas al acabar",
     "hint_low": "ninguna", "hint_high": "muchas",
     "pareja": "training_desire"},
    {"key": "satisfaccion", "label": "A gusto",
     "hint_low": "nada", "hint_high": "mucho"},
)


def escalas_con_manana(checkin_manana: Any) -> list[dict[str, Any]]:
    """Las escalas, cada una con el valor de esa mañana si tiene pareja.

    `manana` es `None` tanto si no hubo check-in como si esa pregunta se dejó
    sin contestar, y la pantalla lo pinta igual -no pone nada-. No se inventa un
    punto de partida: un «esta mañana: 5» que nadie contestó convertiría la
    resta en una afirmación falsa.
    """
    salida = []
    for e in ESCALAS:
        d = dict(e)
        pareja = e.get("pareja")
        d["manana"] = getattr(checkin_manana, pareja, None) if pareja else None
        salida.append(d)
    return salida


# ---------------------------------------------------------------------------
# Los enunciados de las preguntas de elección
# ---------------------------------------------------------------------------
#
# También viajan desde aquí, y no solo sus opciones. La primera versión de la
# pantalla llevaba los enunciados escritos en `despues.js` mientras su propia
# cabecera decía «NO LLEVA NI UNA PREGUNTA ESCRITA»: un comentario que mentía
# en el mismo fichero que describía.
#
# «¿Te pareció corta o larga?» y no «¿Se te quedó corta?», que fue la primera:
# vista en el móvil, la pregunta y su primera opción -«se me quedó corta»- se
# repetían palabra por palabra.

ELECCIONES: tuple[dict[str, Any], ...] = (
    {"key": "cantidad", "enunciado": "¿Te pareció corta o larga?", "opciones": CANTIDAD},
    {"key": "tecnica", "enunciado": "¿Aguantó la técnica?", "opciones": TECNICA},
)

PREGUNTA_MAS_COSTOSO = "¿Cuál te costó más?"
PREGUNTA_FALTA = "¿Por qué?"
SIN_PROBLEMA = "Sin problema"
NINGUNO_EN_ESPECIAL = "Ninguno en especial"
