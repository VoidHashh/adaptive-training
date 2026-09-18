"""Composición del mensaje de Telegram a partir de una `DayDecision`.

Es una función pura: entra la decisión, sale texto. No habla con Telegram. Eso
lo hace `integrations/telegram.py`, que solo sabe enviar cadenas.

La separación no es ceremonia. Significa que `--dry-run` puede imprimir el
mensaje EXACTO que se enviaría sin tocar la red, y que el día que el formato
cambie se pueda comprobar sin mandarse mensajes a uno mismo.

CRITERIO DE CONTENIDO
---------------------
El mensaje se lee a las 7 de la mañana, de pie y con una mano. Por tanto:

- Lo primero es qué hacer hoy, no por qué. El razonamiento va al final.
- Los cambios de progresión se cuentan SIEMPRE y con el número de antes y el
  de después. Un "sube el hip thrust" sin decir de qué a qué obliga a abrir la
  app para saber qué toca, que es justo lo que este sistema evita.
- Lo que NO ha cambiado no se menciona. Doce líneas de "sin cambios" hacen que
  no se lea la que sí importa.
- Las reglas especiales activas se repiten cada día que duran. Que el peso
  muerto lleve nueve días retirado es exactamente lo que hay que recordar el
  noveno día.

UNA REGLA AL INTERPOLAR
-----------------------
Este mensaje sale con `parse_mode=HTML`, y Telegram valida ese HTML de verdad:
si no lo sabe leer devuelve `400 can't parse entities` y NO manda nada. Por eso,
todo lo que entre aquí desde fuera -nombres del `config.yaml`, títulos de Hevy,
motivos, apuntes del motor, el texto de una excepción- pasa por `escapar_html`.
Las etiquetas `<b>`/`<i>` son nuestras y se escriben literales; lo demás, no.

La regla es "escapar por defecto" y no "escapar donde haga falta" porque el
valor peligroso no se sabe mirando el f-string: depende de lo que alguien
escriba algún día en el YAML o de qué excepción reviente. Hoy el `config.yaml`
no tiene ni un `<`, `>` ni `&`, así que esto no arregla nada visible; está para
que el día que lo tenga, el mensaje de las 06:30 siga saliendo.
"""

from __future__ import annotations

from datetime import date, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.engine.luces import NOMBRE_LUZ as _NOMBRE_LUZ
from app.engine.rules import REGLA_SIN_DATOS
from app.engine.sets import warmup_flags
# Sí, un módulo del motor importando de `integrations`. Es deliberado: este
# fichero YA escribe `<b>` y `<i>`, o sea que ya está casado con el dialecto
# HTML de Telegram; lo que no hacía era respetar la otra mitad del contrato, que
# es escapar lo que NO son etiquetas nuestras. La alternativa -copiar aquí las
# tres líneas de `escapar_html`- es exactamente la forma en que una de las dos
# copias se queda vieja. No hay ciclo: `telegram.py` no importa nada del motor.
from app.integrations.telegram import escapar_html, sin_etiquetas

LIMIT = 4096  # límite duro de Telegram

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
EMOJI = {"green": "🟢", "amber": "🟡", "red": "🔴"}

# En versales porque es lo primero que se lee en el móvil a las siete de la
# mañana, y porque el mensaje de Telegram no tiene sitio para más jerarquía que
# el emoji y la mayúscula. Pero se DERIVA de la tabla del motor en vez de
# escribirse otra vez: el día que haya un cuarto color, aquí saldrá solo. Antes
# esta tabla estaba escrita a mano y el proyecto tenía la misma lista de colores
# repetida en cuatro ficheros; el motivo entero está en `app/engine/luces.py`.
NOMBRE_LUZ = {luz: nombre.upper() for luz, nombre in _NOMBRE_LUZ.items()}


def fmt_num(v: float | int | None) -> str:
    """Número con coma decimal, que es como se lee en España."""
    if v is None:
        return "—"
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return f"{float(v):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_date(d: date) -> str:
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


def fmt_short(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}"


def _describe_sets(ex: dict[str, Any], set_cfg: dict[str, Any]) -> str:
    """Resume las series de un ejercicio en una línea legible.

    Agrupa las consecutivas idénticas: `3×12 · 60 kg` en vez de repetir tres
    veces lo mismo. El calentamiento se marca aparte porque no cuenta para el
    trabajo del día y verlo mezclado confunde el volumen real.
    """
    sets = ex.get("sets") or []
    if not sets:
        return "—"
    flags = warmup_flags(sets, set_cfg, ex.get("key"))

    def firma(s: dict[str, Any]) -> tuple:
        return (s.get("reps"), s.get("weight_kg"), s.get("duration_s"))

    def render(grupo: list[dict[str, Any]]) -> str:
        n = len(grupo)
        s = grupo[0]
        if s.get("duration_s"):
            base = f"{n}×{fmt_num(s['duration_s'])} s"
        else:
            base = f"{n}×{fmt_num(s.get('reps'))}"
        peso = s.get("weight_kg")
        if peso:
            base += f" · {fmt_num(peso)} kg"
        return base

    partes: list[str] = []
    # Calentamiento primero, que es el orden en que se ejecuta. Verlo detrás de
    # las series de trabajo obliga a releer la línea para saber por dónde empezar.
    for es_warm in (True, False):
        bloque = [s for s, f in zip(sets, flags, strict=True) if f == es_warm]
        if not bloque:
            continue
        grupos: list[list[dict]] = []
        for s in bloque:
            if grupos and firma(grupos[-1][0]) == firma(s):
                grupos[-1].append(s)
            else:
                grupos.append([s])
        txt = " + ".join(render(g) for g in grupos)
        partes.append(f"cal. {txt}" if es_warm else txt)
    return "  |  ".join(partes)


def _nombre_ejercicio(raw: dict[str, Any], routine: str, key: str) -> str:
    """El nombre legible de un ejercicio, buscado en el config de HOY.

    No se guarda junto a la adopción a propósito: sería una copia del YAML que
    envejece sola. Si el ejercicio ya no existe -se quitó de la rutina- se
    devuelve la clave, que es fea pero cierta.
    """
    for ex in ((raw.get("routines") or {}).get(routine) or {}).get("exercises") or []:
        if ex.get("key") == key:
            return str(ex.get("name") or key)
    return key


def _titulo_rutina(raw: dict[str, Any], key: str | None) -> str:
    """«Día 1», no `dia_1`. Buscado en el config de HOY, como los ejercicios.

    Devuelve la clave cuando no hay título, por el mismo motivo que
    `_nombre_ejercicio`: una rutina retirada del YAML se nombra fea pero cierta,
    y nunca con un hueco. Y devuelve cadena vacía solo si no hay clave, que es el
    caso en que quien llama tiene que decidir no escribir la línea.
    """
    if not key:
        return ""
    titulo = ((raw.get("routines") or {}).get(key) or {}).get("title")
    return str(titulo or key)


def _motivo_adopcion(a: dict[str, Any]) -> str:
    """El porqué de un cambio de carga, o el hueco dicho en voz alta.

    Antes era `a.get('reason', '')`, que dejaba la línea terminada en "62,5 kg —"
    con el guion colgando. Un cambio de carga sin motivo visible es exactamente
    lo que la tabla `load_adoptions` existe para impedir -lo dice su propio
    docstring-, así que cuando falta no se disimula con un guion suelto: se
    nombra. No revienta, porque el peso de hoy sí es correcto y el mensaje de
    las 06:30 tiene que salir igual; lo que no puede es salir aparentando que
    ahí no iba nada.
    """
    motivo = str(a.get("reason") or "").strip()
    return motivo or "sin motivo registrado, que es un fallo: debería haberlo"


def _lineas_adopcion(adopciones: list[dict[str, Any]], raw: dict[str, Any]) -> list[str]:
    """El bloque de "esto lo movió lo que levantaste", o nada si no hay.

    Separa aplicadas de rechazadas porque son dos cosas distintas de leer: una
    dice "el peso de hoy ya no es el que yo había calculado" y la otra "he visto
    un número raro y NO lo he tocado, míralo tú".
    """
    if not adopciones:
        return []

    L: list[str] = []
    aplicadas = [a for a in adopciones if a.get("applied")]
    # Las que ni siquiera llegaron a pesarse van a su propio bloque. Meterlas
    # con las rechazadas obligaría a escribir "se registraron — kg", que es una
    # línea que se lee como un fallo de formato y no como lo que es.
    rechazadas = [
        a
        for a in adopciones
        if not a.get("applied") and a.get("executed_kg") is not None
    ]
    sin_leer = [
        a for a in adopciones if not a.get("applied") and a.get("executed_kg") is None
    ]

    if aplicadas:
        L.append("")
        L.append("🔁 <b>Ajustado a lo que levantaste</b>")
        for a in aplicadas:
            nombre = escapar_html(
                _nombre_ejercicio(raw, str(a.get("routine") or ""), str(a.get("key") or ""))
            )
            flecha = "↑" if a.get("direction") == "up" else "↓"
            L.append(
                f"• {flecha} {nombre}: {fmt_num(a.get('before_kg'))}→"
                f"{fmt_num(a.get('after_kg'))} kg — {escapar_html(_motivo_adopcion(a))}"
            )

    if rechazadas:
        L.append("")
        L.append("🛑 <b>No adoptado</b>")
        for a in rechazadas:
            nombre = escapar_html(
                _nombre_ejercicio(raw, str(a.get("routine") or ""), str(a.get("key") or ""))
            )
            L.append(
                f"• {nombre}: se registraron {fmt_num(a.get('executed_kg'))} kg y "
                f"sigue en {fmt_num(a.get('before_kg'))} kg — "
                f"{escapar_html(_motivo_adopcion(a))}"
            )

    if sin_leer:
        L.append("")
        L.append("❔ <b>Sin nada que leer</b>")
        for a in sin_leer:
            nombre = escapar_html(
                _nombre_ejercicio(raw, str(a.get("routine") or ""), str(a.get("key") or ""))
            )
            L.append(
                f"• {nombre}: {escapar_html(_motivo_adopcion(a))} — sigue en "
                f"{fmt_num(a.get('before_kg'))} kg"
            )
        L.append(
            "Sin un peso apuntado no hay nada que adoptar, ni hacia arriba ni "
            "hacia abajo: esa carga se queda donde está hasta que lo haya."
        )

    return L


def _lineas_sueltos(sueltos: list[dict[str, Any]]) -> list[str]:
    """Los entrenamientos registrados en Hevy que no emparejaban con ningún plan.

    Hasta ahora esto no existía porque el dato tampoco: `run_reconcile` volvía
    sin escribir nada en cuanto el día no era de fuerza o el entrenamiento no
    salía de la rutina prevista, y el entrenamiento desaparecía entero. El
    sistema decidía cada mañana sin saber que el sábado hubo una sesión.

    No es un reproche ni un aviso de fallo: es un acuse de recibo. Lo que
    contesta es "esto lo he visto y lo he contado", que es lo contrario de lo
    único que sabía hacer antes.

    EL MOTIVO VA EN LA LÍNEA, y por eso se escribe al reconciliar y se guarda en
    `workout_log.motivo_suelto` en vez de deducirse aquí: decir que pasó algo sin
    decir el qué obliga a abrir la base de datos para entenderlo, y a las nueve
    de la mañana desde el móvil eso equivale a no avisar.
    """
    if not sueltos:
        return []

    L = ["", "👀 <b>Visto en Hevy, fuera del plan</b>"]
    for s in sueltos:
        nombre = escapar_html(
            str(s.get("title") or s.get("routine") or "entrenamiento sin título")
        )
        dia = escapar_html(str(s.get("day") or ""))
        detalle = []
        if s.get("total_sets"):
            detalle.append(f"{s['total_sets']} series")
        dur = s.get("duration_s")
        if dur:
            detalle.append(f"{int(dur) // 60} min")
        cola = f" ({', '.join(escapar_html(d) for d in detalle)})" if detalle else ""
        porque = f" — {escapar_html(str(s['motivo']))}" if s.get("motivo") else ""
        L.append(f"• {dia}: {nombre}{cola}{porque}")
    return L


# Cómo se llama cada medida del reloj DENTRO DE UNA FRASE, con su artículo.
#
# SEGUNDA COPIA A PROPÓSITO. La primera es `GARMIN` en `app/analysis/series.py`,
# que además lleva unidades, rango y dirección. No se importa de allí porque
# `series.py` arrastra `app.models` y `app.repository`, y el motor no abre la
# base de datos: media docena de docstrings de `decision.py` se apoyan en que
# `decide` es una función pura, y esa propiedad no se rompe por ahorrar cinco
# cadenas.
#
# Que haya dos copias no significa que puedan separarse: `test_message.py`
# compara este diccionario con el de `series.py` y se pone rojo si difieren.
EN_FRASE = {
    "hrv": "la variabilidad",
    "rhr": "el pulso en reposo",
    "sleep_min": "lo que duermes",
    "sleep_score": "la nota de sueño",
    "body_battery": "el Body Battery",
}


def _enumerar(claves: list[str]) -> str:
    """"la variabilidad, el pulso en reposo y la nota de sueño"."""
    nombres = [EN_FRASE.get(c, c) for c in claves]
    if not nombres:
        return ""
    if len(nombres) == 1:
        return nombres[0]
    return f"{', '.join(nombres[:-1])} y {nombres[-1]}"


def _concordar(claves: list[str], singular: str, plural: str) -> str:
    """El verbo que le toca a la enumeración, que unas mañanas es de una medida
    y otras de tres.

    Sin esto salía "la variabilidad y la nota de sueño no se pudo evaluar". Es
    una falta pequeña y es justo la línea que avisa de que el semáforo de hace
    dos horas ya no vale: el sitio donde peor sienta que el mensaje suene a
    plantilla mal rellenada.
    """
    return singular if len(claves) == 1 else plural


# Los tres tipos de sesión en castellano. Era un diccionario suelto dentro de
# `_lineas_sesion`; sale aquí desde que hay un segundo sitio que nombra un tipo
# de sesión -el aviso de anulación-, y dos tablas iguales en dos funciones son
# dos sitios que pueden acabar llamando de forma distinta a lo mismo en el
# mismo mensaje.
NOMBRE_SESION = {
    "full": "sesión completa",
    "reduced": "sesión reducida",
    "recovery": "recuperación",
}


def _lineas_anulada(s: Any) -> list[str]:
    """«Esto lo has pedido tú», dicho en el sitio donde se lee la sesión.

    Sin esta línea, el día que se fuerza la sesión completa en rojo el mensaje
    anuncia una sesión completa bajo una cabecera roja: por fuera, EXACTAMENTE
    lo que se veía con el fallo de `str(action.get("session", FULL))`, que es el
    único de este proyecto que soltaba en vez de frenar. Que esta vez sea a
    petición del usuario no se nota en ningún sitio si no se dice, y quien lea
    el histórico dentro de tres meses lee el mensaje, no el código.

    El aviso de rojo va en negrita y con el motivo delante porque es el caso
    que el usuario quiso poder mirar después: no para impedírselo, para saber
    cuántas veces sube el día que el cuerpo había dicho que parase.
    """
    a = getattr(s, "anulacion", None)
    if a is None:
        return []

    propuesta = NOMBRE_SESION.get(a.propuesta, a.propuesta)
    # El motivo es opcional -se puede anular sin explicarse- y por eso se monta
    # después de saberlo: con el prefijo puesto por delante, un motivo vacío
    # dejaba la frase terminando en «porque: ».
    motivo = f" Dijiste: «{escapar_html(a.motivo)}»." if a.motivo else ""

    if a.forzada_en_rojo:
        return [
            f"⚠️ <b>Esta sesión la has pedido tú.</b> El semáforo está en rojo y "
            f"proponía {escapar_html(propuesta)}; confirmaste subir igual."
            f"{motivo}"
        ]
    return [
        f"✋ <i>Esta sesión la has pedido tú: el sistema proponía "
        f"{escapar_html(propuesta)}.{motivo}</i>"
    ]


def _lineas_sesion(s: Any, raw: dict, set_cfg: dict[str, Any]) -> list[str]:
    """El bloque que prescribe: qué sesión tocaría hoy y con qué series.

    Estaba escrito en línea dentro de `render_telegram` y sale aquí por una razón
    concreta: desde que existe la pregunta «¿vas a entrenar hoy?» este bloque es
    CONDICIONAL, y la alternativa era envolver cincuenta líneas -con sus cuatro
    comentarios largos- en un `if`. Una condición que se lee de un vistazo vale
    más que un diff pequeño; y con el bloque suelto, el sitio donde se decide
    prescribir o no es una sola línea en el sitio obvio.

    AQUÍ HABÍA UNA RAMA PARA `rest`, `pool` Y `bike`, Y EL MODO VERBAL ERA OTRO
    --------------------------------------------------------------------------
    Con calendario fijo, los días sin fuerza asignada salían como un "😴
    Descanso" y los de fuerza se anunciaban en indicativo -"Día 1 (sesión
    completa)"-, como quien lee una agenda. Ya no hay días asignados ni días de
    descanso decididos por el sistema: todos los días tienen la rutina que toque
    en el ciclo, y el mensaje no manda, informa de qué tocaría SI se va al
    gimnasio. Quién decide es el usuario, y el sistema se entera leyendo Hevy.

    El día rojo se queda en indicativo a propósito: el bloque de recuperación no
    es una sesión del ciclo que uno elija hacer o no, es lo que el sistema
    propone para hoy. Ponerle un "si vas al gimnasio" delante lo ofrecería como
    alternativa al gimnasio, que es lo contrario de lo que dice un rojo.
    """
    etiqueta = NOMBRE_SESION.get(s.kind, s.kind)

    if s.kind == "recovery":
        cab = f"💪 <b>{escapar_html(s.title)}</b> ({escapar_html(etiqueta)})"
    else:
        cab = (
            f"💪 <b>Si vas al gimnasio hoy:</b> {escapar_html(s.title)} "
            f"({escapar_html(etiqueta)})"
        )
    # `routines.*.focus` llevaba desde el principio en el YAML sin que lo
    # leyera nadie: "Tren inferior + core", "Cadena posterior + espalda",
    # "Caderas + hombro + brazo + core". Es la única frase del fichero que
    # dice de qué va la sesión, y el mensaje de la mañana la ignoraba
    # mientras enumeraba ocho ejercicios sin encabezarlos.
    #
    # Va pegado al título y no en una línea aparte: el mensaje ya tiene
    # bloques de sobra, y esto es un subtítulo, no un apartado. Se busca en
    # `routines` a propósito: en un día de recuperación `routine_key`
    # apunta a `recovery_blocks`, no encuentra nada, y el encabezado se
    # queda como estaba.
    foco = ((raw.get("routines") or {}).get(s.routine_key or "") or {}).get("focus")
    if foco:
        cab += f" — {escapar_html(foco)}"

    # Pegado a la cabecera y antes de los ejercicios: la lista que viene debajo
    # es la de la sesión pedida, y hay que saber de quién es la sesión ANTES de
    # leerla, no después de haberla leído como si la propusiera el sistema.
    out = ["", cab, *_lineas_anulada(s)]
    for ex in s.exercises:
        nombre_ex = escapar_html(ex.get("name", ex.get("key")))
        out.append(f"• {nombre_ex} — {_describe_sets(ex, set_cfg)}")
    if s.hiit_block:
        # El título, no la clave. Salía `hiit_dia_2` en el mensaje de la mañana
        # -el único sitio donde esto se lee- mientras el YAML tenía «Día 2 HIIT»
        # dos líneas más abajo del `hevy_routine_id`. Los bloques HIIT viven en
        # `routines` como cualquier otra rutina, así que el mismo buscador que
        # traduce `dia_1` los traduce a ellos sin nada nuevo.
        out.append(
            f"🔥 <b>HIIT (entreno aparte):</b> "
            f"{escapar_html(_titulo_rutina(raw, s.hiit_block))}"
        )
        # Y SUS EJERCICIOS DEBAJO, que antes salían arriba y revueltos.
        #
        # El bloque se concatenaba a la lista de fuerza, así que el wall ball
        # aparecía como una viñeta más entre la prensa y las planchas, y al
        # final una línea suelta decía «HIIT: Día 1 HIIT» sin decir cuáles de
        # las doce viñetas eran. Ahora son dos entrenamientos distintos en Hevy
        # y hay que empezar los dos por separado, así que el mensaje -que es lo
        # único que se lee antes de abrir la app- tiene que enseñar dónde
        # acaba uno y empieza el otro.
        for ex in (s.hiit.exercises if getattr(s, "hiit", None) is not None else []):
            nombre_ex = escapar_html(ex.get("name", ex.get("key")))
            out.append(f"• {nombre_ex} — {_describe_sets(ex, set_cfg)}")
    return out


def _lineas_sin_entreno(decision: Any, raw: dict) -> list[str]:
    """Lo que ocupa el sitio de la sesión el día que has dicho que no vas.

    Y ocupa su sitio, no lo deja vacío. Un mensaje al que simplemente le falta el
    bloque de la sesión se lee como un mensaje roto -¿se ha caído algo?, ¿se ha
    olvidado de qué toca hoy?- y la duda acaba en abrir Hevy a comprobarlo, que
    es justo el trabajo que este sistema existe para ahorrar.

    Las dos frases contestan las dos preguntas que quedan en el aire, y no hay
    una tercera a propósito:

    1. «¿Pierdo el turno?» No. La rotación la manda lo ÚLTIMO EJECUTADO, no el
       calendario, así que la rutina que tocaba sigue siendo la siguiente
       mañana, la semana que viene o en octubre. No hace falta que el motor haga
       nada para eso: hace falta decirlo, porque el sistema anterior sí perdía
       sesiones y nadie se fía de lo que no se le cuenta.
    2. «¿Y si cambio de idea a las siete de la tarde?» Está escrita en Hevy. La
       rutina se escribe igual -eso no se toca-, y decirlo aquí es lo que hace
       que decir «no» por la mañana no sea una puerta cerrada.

    No hay una línea animándote a ir, ni recordándote lo que llevas de semana, ni
    preguntando si estás seguro. Has contestado una pregunta que el sistema hizo;
    discutir la respuesta enseñaría a no contestarla.
    """
    rutina = getattr(decision, "rotation_routine", None)
    titulo = ((raw.get("routines") or {}).get(rutina or "") or {}).get("title")

    cual = (
        f"{escapar_html(titulo)} sigue siendo la siguiente"
        if titulo
        else "la rutina que tocaba sigue siendo la siguiente"
    )
    return [
        "",
        f"🌙 <b>Hoy no entrenas.</b> {cual}: la rotación no se mueve hasta que se haga.",
        "   <i>La rutina está escrita en Hevy de todas formas, por si cambias de "
        "idea.</i>",
    ]


def _lineas_anulacion(decision: Any, tz: str | None) -> list[str]:
    """El aviso de que esta decisión deja sin efecto la de hace un rato.

    Se pinta SIEMPRE que ha habido recomputación, no solo cuando cambia el
    color. El motivo es que la recomputación manda un segundo mensaje del mismo
    día: si llegara sin explicarse, el usuario tendría dos semáforos en el móvil
    y ninguna forma de saber cuál manda. Un mensaje duplicado y mudo es peor que
    uno que se presenta.

    Lo que sí cambia con el color es el tono y el sitio. Si cambia, esto es la
    noticia del día y va en negrita justo debajo de la cabecera; si no cambia,
    es un acuse de recibo en cursiva.
    """
    a = getattr(decision, "anulacion", None)
    if a is None:
        return []

    # `computed_at` se guarda en UTC y aquí se lee una hora de reloj de pared.
    # Sin convertir, el mensaje de las 09:00 diría "anula la de las 04:23" y el
    # usuario no reconocería su propio check-in de las 06:23.
    #
    # El "de las" se monta DESPUÉS de saber la hora y no antes: `_hora_local`
    # devuelve cadena vacía cuando no puede resolver la zona, y con el prefijo
    # puesto por delante el mensaje salía "anula la decisión de las , que salió
    # verde". Renunciar a la hora es aceptable; enseñar el hueco no.
    reloj = _hora_local(getattr(a, "decidida_a", None), tz)
    hora = f" de las {reloj}" if reloj else ""

    anterior = _NOMBRE_LUZ.get(a.anterior, a.anterior)
    medidas = list(a.medidas)
    llegado = _enumerar(medidas)
    cola = ""
    if a.sin_llegar:
        falta = list(a.sin_llegar)
        cola = (
            f" {_concordar(falta, 'Sigue', 'Siguen')} sin llegar "
            f"{_enumerar(falta)}, así que esta decisión tampoco está completa."
        )

    if a.cambia_el_color(decision.light):
        return [
            f"♻️ <b>Esto anula la decisión{escapar_html(hora)}, que salió "
            f"{escapar_html(anterior)}.</b> A esa hora el reloj todavía no había "
            f"subido la noche y {escapar_html(llegado)} "
            f"{_concordar(medidas, 'no se pudo', 'no se pudieron')} evaluar. "
            f"{_concordar(medidas, 'Con el dato ya puesto', 'Con los datos ya puestos')}"
            f", el semáforo cambia.{escapar_html(cola)}"
        ]
    return [
        f"♻️ <i>Recalculado con {escapar_html(llegado)}, que a la hora del "
        f"check-in todavía no {_concordar(medidas, 'estaba', 'estaban')}. El "
        f"semáforo no cambia.{escapar_html(cola)}</i>"
    ]


def _lineas_sin_datos(decision: Any) -> list[str]:
    """«Esto es ámbar porque no he podido mirar», dicho con esas palabras.

    Va arriba, pegado a la cabecera y justo debajo de la anulación si la hay, y
    NO dentro de «Decidido con datos incompletos», que es donde cabría por
    parecido. Aquel bloque enumera degradaciones de una decisión que ya está
    tomada; esto ES la decisión: explica el color que encabeza el mensaje. Leído
    treinta líneas más abajo, entre seis apuntes, el usuario habría recortado la
    sesión un cuarto sin saber que el motivo era un hueco y no su cuerpo.

    Tampoco va dentro de «Por qué», que es el sitio natural para la regla que
    dispara, por una razón tonta y decisiva: ese bloque se apaga con
    `include_reasoning: false`, y entonces el único ámbar del sistema que no
    significa «estás peor» se quedaría sin explicación ninguna.

    Las medidas se nombran una a una en vez de con una frase fija. La frase fija
    -«no se han podido evaluar HRV, pulso en reposo ni sueño»- diría tres cosas
    las mañanas en que solo falta una, y a las dos semanas el aviso se leería
    como una plantilla y no como un dato.
    """
    disparo = next(
        (r for r in decision.light_decision.fired if r.name == REGLA_SIN_DATOS),
        None,
    )
    if disparo is None:
        return []

    medidas = list(disparo.missing)
    if not medidas:  # pragma: no cover - el motor nunca lo produce vacío
        que = "el bienestar"
    else:
        que = _enumerar(medidas)
    return [
        "",
        f"🟡 <b>Ámbar por precaución: no {_concordar(medidas, 'se ha', 'se han')} "
        f"podido evaluar {escapar_html(que)}.</b> A esta hora el reloj todavía no "
        f"había subido la noche. No es que estés peor: es que no se ha podido "
        f"mirar, y no mirar no se pinta de verde. Si el dato llega luego, el día "
        f"se recalcula y te aviso.",
    ]


def _hora_local(cuando: Any, tz: str | None) -> str:
    """HH:MM en la hora del usuario, a partir de un sello guardado en UTC.

    `decisions.computed_at` es UTC y sin `tzinfo` -lo escribe SQLite-, así que
    hay que ponérselo antes de convertir: un `astimezone()` sobre un ingenuo lo
    interpretaría como hora local y no movería nada, que es exactamente el fallo
    de dos horas por el que se leyó una vez las 04:30 del registro como un
    evento distinto de las 06:30. Aquí ese fallo no sería mío: sería un mensaje
    diciéndole al usuario que anula un check-in que él recuerda haber mandado
    dos horas después.

    La zona sale del `config.yaml` y no de una constante. Si no se puede
    resolver se devuelve cadena vacía y la frase se queda sin la hora, que es
    peor mensaje pero no es un mensaje FALSO.
    """
    if not tz:
        return ""
    try:
        if cuando.tzinfo is None:
            cuando = cuando.replace(tzinfo=timezone.utc)
        return cuando.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    except (AttributeError, ValueError, OSError, ZoneInfoNotFoundError):
        return ""


def render_telegram(decision: Any, config: Any = None) -> str:
    """El mensaje completo del día."""
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    set_cfg = raw.get("set_types", {}) or {}
    notif = ((raw.get("notifications") or {}).get("telegram") or {})
    incluir_motivo = bool(notif.get("include_reasoning", True))

    L: list[str] = []
    luz = decision.light
    # Sin `.get(luz, ...)`, y este es el sitio donde más importa. El semáforo es
    # la cabecera del mensaje y el resumen de la decisión entera del día. Con el
    # defecto puesto, una luz desconocida -un `config.yaml` con un valor nuevo,
    # una regla que devuelve otra cosa, un typo- salía como "⚪ ... — PURPLE" y
    # el mensaje seguía adelante tan normal, pidiendo entrenar bajo un semáforo
    # que no existe. Degradar en silencio justo aquí es lo contrario de lo que
    # tiene que hacer un sistema que decide solo.
    if luz not in EMOJI or luz not in NOMBRE_LUZ:
        raise ValueError(
            f"semáforo desconocido: {luz!r}. Las luces válidas son "
            f"{sorted(EMOJI)}. No se manda el mensaje con una luz inventada: "
            f"es la línea que resume la decisión del día."
        )
    L.append(
        f"{EMOJI[luz]} <b>{fmt_date(decision.day).capitalize()} — "
        f"{NOMBRE_LUZ[luz]}</b>"
    )

    # Justo debajo de la cabecera y antes que nada más, incluida la descarga: si
    # el semáforo de hace dos horas ya no vale, eso se lee ANTES que la sesión
    # que ese semáforo condiciona. Ponerlo al final, entre los apuntes, sería
    # dejar que se lea el plan del día creyendo que es el primero.
    L.extend(_lineas_anulacion(decision, raw.get("timezone")))

    if decision.deload.active:
        # El motivo va aquí, pegado al aviso, y no suelto entre los apuntes:
        # "semana 8 del programa" contesta la pregunta que provoca el 🔻.
        motivo_dl = (
            f" — {escapar_html(decision.deload.reason)}"
            if decision.deload.reason else ""
        )
        L.append(f"🔻 <i>Semana de descarga{motivo_dl}</i>")

    L.extend(_lineas_sin_datos(decision))

    # --- la sesión ----------------------------------------------------------
    s = decision.session

    # PRESCRIBIR O INFORMAR, QUE ES LA ÚNICA DECISIÓN QUE TOMA ESTE FICHERO HOY.
    #
    # Con un «no voy» contestado esta mañana, el mensaje deja de darte el plan
    # del día y pasa a contarte el día: estado, tendencia y bici si toca. No
    # desaparece nada más. Las adopciones de anoche, los entrenos sueltos, las
    # reglas activas, el recuento de intensas, los avisos de datos incompletos y
    # la recalibración siguen saliendo, porque ninguno de esos es una
    # prescripción: son cosas que han pasado o que hay que saber.
    #
    # Se compara con `is not False` y no con `if decision.va_a_entrenar` porque
    # los tres estados no son dos. `None` -no has contestado, o es un día del
    # archivo anterior a que la pregunta existiera- tiene que seguir prescribiendo
    # como siempre. Escrito `if decision.va_a_entrenar:` el sistema entero dejaría
    # de proponer sesión todos los días en que no se rellena el formulario, que
    # son la mayoría, y lo haría en silencio.
    prescribe = getattr(decision, "va_a_entrenar", None) is not False

    if prescribe:
        L.extend(_lineas_sesion(s, raw, set_cfg))
    else:
        L.extend(_lineas_sin_entreno(decision, raw))

    # --- lo que movió la carga que se levantó de verdad ---------------------
    # Va ANTES de "Sube hoy" porque ocurrió antes: la adopción se decide al
    # reconciliar por la noche y fija el punto de partida desde el que la mañana
    # ha progresado. Leerlo al revés haría que un "62,5→65" pareciera contradecir
    # un objetivo que dos líneas más arriba era 60.
    #
    # FUERA de `include_reasoning`, como los avisos: esto no es "por qué he
    # decidido esto", es un cambio en el peso que está escrito en Hevy ahora
    # mismo. Y las RECHAZADAS salen igual que las aplicadas, que es la mitad que
    # importa: un tope que actúa en silencio deja un ejercicio quieto sin que
    # nadie pueda saber por qué.
    L.extend(_lineas_adopcion(getattr(decision, "load_adoptions", None) or [], raw))

    # FUERA de `include_reasoning`, por lo mismo que las adopciones: no es "por
    # qué he decidido esto", es "esto que hiciste lo he visto y lo he contado".
    L.extend(_lineas_sueltos(getattr(decision, "entrenos_sueltos", None) or []))

    # --- lo que ha cambiado hoy --------------------------------------------
    # `prescribe` además de la lista vacía, y no porque hoy pueda no estarlo: con
    # un «no voy», `evaluate_gate` cierra la puerta antes que nada y el bucle de
    # `plan_progression` sale por `continue` sin tocar un solo ejercicio, así que
    # `changes` ya viene vacío. El `if` no quita ninguna línea que hoy exista.
    #
    # Está por lo que pasaría si dejara de venir vacío. Tal y como estaba, este
    # bloque hereda de `progression.py` la promesa de no prescribir; el día que
    # alguien mueva esa comprobación de sitio -o añada un modo que proponga algo
    # con la puerta cerrada-, el fallo no sale en `progression.py`, sale aquí, en
    # forma de "📈 Sube hoy" debajo de un "🌙 Hoy no entrenas". Este fichero
    # decide si prescribe o no; que lo decida entero.
    #
    # LAS DOS PROGRESIONES. El bloque HIIT tiene la suya desde que viaja como
    # sesión aparte, y `plancha_frontal` es el único ejercicio del programa que
    # sube por volumen. Si esa subida no se nombra aquí, el usuario se encuentra
    # la plancha a 35 s en Hevy sin que nadie le haya dicho que hoy subía: un
    # cambio de carga silencioso, que es justo lo que este bloque existe para
    # evitar. Van en la misma lista y no en dos secciones porque lo que el
    # apartado contesta -"¿qué sube hoy?"- es una sola pregunta, y el nombre del
    # ejercicio ya dice de qué entreno es.
    prog_hiit = getattr(decision, "progression_hiit", None)
    cambios = list(decision.progression.changes if decision.progression else [])
    cambios += prog_hiit.changes if prog_hiit is not None else []
    if prescribe and cambios:
        L.append("")
        L.append("📈 <b>Sube hoy</b>")
        for e in cambios:
            L.append(f"• {escapar_html(e.text())}")

    # --- lo que lleva parado ------------------------------------------------
    # FUERA de `include_reasoning`, por lo mismo que los bloques de más abajo:
    # que un ejercicio haya dejado de progresar no es "por qué he decidido
    # esto", es un hecho del programa que solo se puede arreglar a mano.
    #
    # Esto no se pintaba. `ProgressionPlan` generaba las líneas desde el primer
    # día y el único que las leía era un script de diagnóstico. El resultado
    # medido: en la simulación de doce semanas, `press_triceps_sentado` y
    # `remo_t_apoyado` no subieron ni una vez -esperaban una carga que nadie
    # había apuntado en Hevy- y ninguno de los mensajes de esos ochenta y
    # cuatro días lo mencionó. El sistema lo sabía y no lo dijo, que es la
    # forma más cara de saberlo.
    #
    # SIN `prescribe`, y es la excepción deliberada entre los bloques de
    # progresión. "Sin progresión por falta de carga registrada en Hevy" no te
    # dice qué hacer hoy: te dice que hay un ejercicio esperando un dato desde
    # hace semanas y que va a seguir esperándolo. Eso se lee igual de bien el día
    # que no vas al gimnasio -mejor, incluso: es el día que hay tiempo de
    # arreglarlo-. La línea que separa lo que se calla de lo que no es
    # prescripción contra hecho, no "todo lo que venga de progression.py".
    # También las dos, y aquí importa más todavía: el techo de la plancha son
    # 60 s y el YAML sale de 30, así que es el ejercicio del programa que antes
    # va a tocar techo. "Toca cambiar el ejercicio" dicho el día que ocurre es
    # accionable; no dicho, la plancha se queda clavada en 60 s para siempre y
    # parece que el sistema la ha olvidado.
    parados = list(decision.progression.stopped_lines() if decision.progression else [])
    parados += prog_hiit.stopped_lines() if prog_hiit is not None else []
    if parados:
        L.append("")
        L.append("⏸️ <b>Sin progresar</b>")
        for linea in parados:
            L.append(f"• {escapar_html(linea)}")

    # Retiradas y recortes: son cambios que el usuario notará en la app y que
    # sin explicación parecen un fallo del sistema.
    #
    # Este sí lleva `prescribe` por necesidad, no por prudencia: `s.dropped` lo
    # llena el constructor de la sesión mirando el semáforo, y le da igual lo que
    # hayas contestado esta mañana. Un ámbar con dos ejercicios recortados los
    # tiene puestos también el día que dices que no vas. Sin el `if`, el mensaje
    # sacaría "➖ Fuera hoy: sentadilla búlgara, hip thrust" sin haber enseñado
    # antes ninguna sesión de la que sacarlos: fuera ¿de qué?
    if prescribe and s.dropped:
        L.append("")
        L.append(
            f"➖ <b>Fuera hoy:</b> "
            f"{', '.join(escapar_html(d) for d in s.dropped)}"
        )

    # --- reglas especiales vigentes ----------------------------------------
    reglas = [r for r in decision.active_rules if r.name != "semana_de_descarga"]
    if reglas:
        L.append("")
        L.append("⚠️ <b>Reglas activas</b>")
        for r in reglas:
            hasta = f" (hasta el {fmt_short(r.active_until)})" if r.active_until else ""
            nombre_r = escapar_html(r.name.replace("_", " ").capitalize())
            L.append(f"• {nombre_r}{hasta}")

    # --- bici ---------------------------------------------------------------
    # `se_muestra` y no `applies`. Son cosas distintas desde que se quitó el
    # calendario: `applies=False` ya no significa "hoy no es día de bici" -eso
    # era un no evento y por eso no se decía-, sino "no se ha podido calcular el
    # punto de partida contra tu histórico". Eso sí se dice, con su motivo, y
    # con las notas de contexto debajo igual que cualquier otro día.
    if decision.bike is not None and decision.bike.se_muestra:
        L.append("")
        L.append(f"🚴 {escapar_html(decision.bike.text())}")
        # DE DÓNDE SALE EL NIVEL, EN CRISTIANO Y TODOS LOS DÍAS.
        #
        # Esta línea no existía y el número caía del cielo. El punto de partida
        # ya no es una constante del YAML que uno pueda ir a mirar: es un
        # cálculo contra el propio histórico que cambia cada día, así que sin
        # esto el mensaje afirma «intensa» y no hay forma de saber por qué.
        #
        # Va en cursiva y sin viñeta a propósito: no es un hecho de contexto
        # -esos van debajo, con `·`, y NO han entrado en la decisión-, es
        # literalmente la razón del nivel de arriba. Mezclarla con las notas
        # borraría la única distinción que este bloque se ha ganado a pulso.
        #
        # Y aquí es donde se ve el freno del parón largo, que es una de las dos
        # cosas por las que existe la recomendación. Antes el mensaje ponía
        # «Media» a secas después de seis semanas parado: el freno actuaba y no
        # se veía. Ahora dice que llevas bastante más de lo que sueles esperar y
        # que al volver toca volumen antes que carga.
        if decision.bike.baseline_en_claro:
            L.append(f"   <i>{escapar_html(decision.bike.baseline_en_claro)}</i>")
        # Los hechos de contexto van con viñeta y DEBAJO, separados del nivel
        # recomendado. Pegados a la misma línea se leerían como el motivo del
        # nivel, y no lo son: ninguno ha entrado en la decisión. Es la misma
        # distinción que hay en el código entre `downgrades` y `notas`, y tiene
        # que sobrevivir hasta la pantalla del móvil o no sirve de nada.
        for nota in decision.bike.texto_notas():
            L.append(f"   · {escapar_html(nota)}")

    # --- lo que llevas hecho esta semana ------------------------------------
    # TODOS los días, no solo los de bici, y sin denominador.
    #
    # Este bloque es lo que queda del presupuesto semanal de intensas. Antes el
    # número solo aparecía cuando además servía para recortar la salida del
    # sábado, así que de lunes a viernes el sistema lo sabía y no lo decía. Un
    # dato que solo se enseña cuando te frena no es información, es la
    # justificación del frenazo; y justamente ahora que no frena nada es cuando
    # tiene que estar todos los días.
    #
    # AQUÍ HABÍA UN `and not ya_en_la_bici`, Y ERA UNA BOMBA DE RELOJERÍA
    # ---------------------------------------------------------------------
    # El recuento vivía en dos sitios: como nota de la bici los fines de semana
    # y como esta línea el resto de días, y esta condición elegía uno para no
    # repetirlo. Mientras la bici solo hablara sábado y domingo funcionaba.
    #
    # Al quitar el calendario, la bici habla todos los días, así que esta línea
    # dejaba de salir NUNCA y el recuento pasaba a depender enteramente de la
    # nota. Y la nota se fabrica cuando se construye la recomendación, o sea
    # antes: cualquier ruta que rellenara `intense_count` después de `decide()`
    # perdía el recuento del mensaje entero, sin error, sin hueco y sin que nada
    # se pareciera a un fallo. Un dato que se enseña todos los días no puede
    # depender del orden en que se construyen dos objetos.
    #
    # Ahora el recuento sale de aquí y solo de aquí, leyendo la señal en el
    # momento de escribir. La bici ya no lo lleva en sus notas.
    #
    # Se lee con punto y no con `getattr(..., None)` a propósito. `signals` es
    # un campo obligatorio de `DayDecision` e `intense_count` es un campo de
    # `Signals`: si alguno de los dos deja de existir, eso es un fallo y tiene
    # que sonar. Un `getattr` con defecto aquí convertiría el día que se rompa
    # la señal en un mensaje sin la línea, o sea exactamente lo que este bloque
    # existe para que no pase. El None legítimo -una ruta que no construye
    # señales- sigue teniendo su sitio: es el valor por defecto del campo.
    conteo = decision.signals.intense_count
    if conteo is not None:
        L.append("")
        L.append(f"🔥 {escapar_html(conteo.linea().capitalize())}")

    # --- lo que no se ve mirando un solo día --------------------------------
    # FUERA de `include_reasoning`, y es el bloque donde más claro está por qué.
    # Esto no explica la decisión de hoy: no ha entrado en ella. Ninguna regla
    # del semáforo mira más de tres días atrás, así que "llevas seis días sin un
    # verde" es información que el motor NO ha usado para decidir y que solo
    # existe en esta línea. Apagar el razonamiento es decir "no me cuentes por
    # qué has decidido esto", no "no me cuentes hacia dónde voy".
    #
    # Va después de la bici y antes de los avisos de datos incompletos porque es
    # lo último del plan y lo primero de las advertencias: cierra el "qué hago
    # hoy" y abre el "con qué fiabilidad te lo estoy diciendo".
    tendencia = getattr(decision, "tendencia", None)
    lineas_tendencia = tendencia.lineas() if tendencia is not None else []
    if lineas_tendencia:
        L.append("")
        L.append("📉 <b>Tendencia</b>")
        for linea in lineas_tendencia:
            # El prefijo "Tendencia:" que trae cada línea ya está en la cabecera
            # del bloque. Repetirlo ocho palabras más abajo solo gasta pantalla.
            L.append(f"• {escapar_html(linea.removeprefix('Tendencia: '))}")

    # --- decidido con datos incompletos -------------------------------------
    # FUERA de `include_reasoning` a propósito. Apagar el razonamiento es
    # decir "no me cuentes por qué", no "ocúltame que hoy has decidido a
    # ciegas". Un día sin check-in, sin línea base de HRV o con salidas del
    # fin de semana sin clasificar es un día en el que el semáforo vale
    # menos, y eso hay que saberlo aunque no se quiera leer el resto.
    #
    # Se vuelca `signals.notes` entero: TODO lo que se apunta ahí es una
    # degradación (sin check-in, sin línea base, sin serie para el umbral
    # adaptativo, histórico insuficiente, percentil 0, salidas sin
    # clasificar). Si algún día se apunta ahí algo que no lo sea, va a
    # aparecer en el mensaje y se verá: mejor un aviso de más que uno que
    # solo existía en el log del servidor.
    degradaciones = list(decision.signals.notes)

    # Una regla que no se pudo evaluar NO es una regla que no disparó, y la
    # diferencia es justo la que este bloque existe para contar: hoy el
    # semáforo se ha decidido sin mirar `cervicales_hombros` porque faltaba
    # `upper_discomfort`, y esa es una regla que podía haber puesto el día en
    # ámbar. Estaba escrito solo dentro de `include_reasoning`, así que con el
    # razonamiento apagado un verde decidido sin mirar media hoja de reglas se
    # leía como un verde con todas miradas.
    #
    # Solo se nombran las reglas, no cada señal que faltaba. Las señales
    # derivadas (hrv_ratio, hrv_baseline, ...) multiplican la lista sin añadir
    # nada: si falta el HRV faltan las tres, y lo accionable es "hoy no se pudo
    # mirar el HRV".
    sin_datos = sorted({r.name for r in decision.light_decision.skipped})
    if sin_datos:
        cabe = sin_datos[:4]
        resto = len(sin_datos) - len(cabe)
        cola = f" (+{resto})" if resto > 0 else ""
        degradaciones.append(f"sin datos para evaluar: {', '.join(cabe)}{cola}")

    if degradaciones:
        L.append("")
        L.append("🔍 <b>Decidido con datos incompletos</b>")
        for n in degradaciones:
            L.append(f"• {escapar_html(n)}")

    # --- cuánto hace de la última sesión de fuerza --------------------------
    # Aquí había un bloque "⏳ Sesión perdida" que saltaba cuando un
    # aplazamiento caducaba: "esta semana has entrenado una vez menos". No hay
    # sesiones perdidas porque no hay aplazamiento -la que no se hace sigue
    # siendo la siguiente-, y lo que ocupa su sitio es este número.
    #
    # SALE SIEMPRE, SIN UMBRAL. La tentación era enseñarlo solo a partir de N
    # días, y ese N habría sido una constante inventada: ni sale de los datos
    # del usuario ni hay nada que la justifique. Un número que aparece a los
    # ocho días y no a los siete está dando una opinión disfrazada de dato.
    # Diciéndolo todos los días no hay opinión: hay una cuenta, y quien la lee
    # sabe mejor que el sistema si nueve días son unas vacaciones o un aviso.
    #
    # Y por eso tampoco lleva emoji de alarma ni va en su propio bloque con
    # título: es un apunte, como el recuento de intensas. El tono importa aquí
    # más que en ningún otro sitio del mensaje, porque es la única línea que
    # habla de lo que NO se ha hecho.
    if decision.last_strength:
        _, ultimo_dia = decision.last_strength
        dias = (decision.day - ultimo_dia).days
        if dias == 0:
            L.append("")
            L.append("💤 <i>Última sesión de fuerza: hoy mismo.</i>")
        else:
            L.append("")
            L.append(
                f"💤 <i>Última sesión de fuerza: hace {dias} "
                f"{'día' if dias == 1 else 'días'} "
                f"({fmt_short(ultimo_dia)}).</i>"
            )
    else:
        # Ninguna sesión del ciclo leída todavía. Se dice, y no se calla: el
        # silencio aquí se confundiría con "hoy mismo", que es el caso opuesto.
        L.append("")
        L.append(
            "💤 <i>Todavía no hay ninguna sesión de fuerza leída de Hevy; "
            "la rotación empieza por el principio.</i>"
        )

    # --- lo que lleva más de una vuelta sin hacerse -------------------------
    # La línea de arriba dice cuánto hace de la ÚLTIMA sesión, sea cual sea.
    # Ésta dice algo que aquella no puede ver: que entrenando con normalidad,
    # tres veces por semana, hay una rutina concreta que no ha entrado.
    #
    # NO ES UNA COLA NI UN AVISO DE DEUDA. No hay nada que reponer: el ciclo da
    # la vuelta y esa rutina vuelve a tocar cuando le toque, sin que nadie la
    # adelante. Esto es información sobre uno mismo -saltarse el Día 1 varias
    # veces seguidas dice algo-, y por eso se cuenta y no se corrige.
    #
    # UNA SOLA, LA QUE MÁS LLEVA PARADA. `pendientes` viene ordenada por eso. Si
    # hay dos paradas, enumerar las dos convierte un apunte en una lista de
    # tareas, que es exactamente el tono que se pidió evitar; y con un ciclo de
    # tres, que haya dos paradas ya lo dice la de arriba contando días.
    #
    # Y la caducada no se nombra: ver `CADUCA_TRAS` en `rotacion.py`. Una línea
    # que sale todas las mañanas durante meses deja de informar y empieza a
    # sonar a reproche por pura insistencia. El dato sigue guardándose en la
    # decisión; lo que caduca es decirlo en voz alta, no saberlo.
    #
    # EL EMOJI NO ES 🔁 AUNQUE LO PAREZCA. Ése ya es el de «Ajustado a lo que
    # levantaste», que habla de kilos y no de rotación; repetirlo haría que dos
    # bloques sin nada que ver se leyeran como el mismo tipo de aviso en un
    # mensaje que se ojea en diez segundos.
    vivas = [p for p in (getattr(decision, "pendientes", None) or []) if not p.caducada]
    if vivas:
        p = vivas[0]
        L.append(
            f"🗓 <i>{escapar_html(_titulo_rutina(raw, p.clave))}: han pasado "
            f"{p.sesiones_desde} sesiones de fuerza desde la última vez "
            f"({fmt_short(p.ultima_vez)}).</i>"
        )

    # --- mantenimiento del propio sistema -----------------------------------
    # FUERA de `include_reasoning`, y aquí el motivo es el más claro de todos:
    # esto no es razonamiento, ni siquiera es sobre el entrenamiento de hoy. Es
    # el sistema pidiendo que le miren unos números que se calibraron sobre
    # muestras cortas y que llevan desde entonces decidiendo mañanas.
    #
    # Va al final, después de todo lo que hay que hacer hoy y de todos los
    # avisos sobre la fiabilidad de la decisión, porque es lo único del mensaje
    # que no tiene prisa. Y no va dentro de "Decidido con datos incompletos",
    # que es donde cabría por parecido: aquello dice "hoy he decidido peor de lo
    # normal"; esto dice "llevo un mes decidiendo con unos umbrales que
    # prometiste revisar". Mezclarlos haría que el segundo se leyera como una
    # degradación de hoy y se descartara con ella.
    recalibracion = getattr(decision, "recalibracion", None)
    lineas_recal = recalibracion.lineas() if recalibracion is not None else []
    if lineas_recal:
        L.append("")
        L.append("🛠 <b>Toca recalibrar</b>")
        for linea in lineas_recal:
            L.append(f"• {escapar_html(linea)}")

    # --- por qué ------------------------------------------------------------
    if incluir_motivo:
        L.append("")
        L.append("<i>Por qué</i>")
        if decision.trigger_rule:
            disparo = next(
                (r for r in decision.light_decision.fired
                 if r.name == decision.trigger_rule),
                None,
            )
            det = escapar_html(
                "; ".join(disparo.detail)
            ) if disparo and disparo.detail else ""
            regla = escapar_html(decision.trigger_rule)
            L.append(f"• {regla}{f': {det}' if det else ''}")
        else:
            L.append("• Ninguna regla ha saltado hoy")

        if decision.progression and not decision.progression.gate_open:
            # "Progresión cerrada: primera vez que el sistema ve el Día 2" son
            # dos malas noticias donde no hay ninguna. El rótulo se quita
            # cuando lo único que pasa es que la rutina se estrena hoy: el
            # motivo ya se explica solo y no necesita que lo presenten como una
            # puerta que se cierra.
            motivo = escapar_html(decision.progression.gate_reason)
            if getattr(decision.progression, "estreno", False):
                L.append(f"• {motivo[:1].upper()}{motivo[1:]}")
            else:
                L.append(f"• Progresión cerrada: {motivo}")

        # El resto de apuntes del motor. No son degradaciones -por eso van
        # aquí y no arriba- pero tampoco eran visibles en ninguna parte: solo
        # los imprimía el CLI, que es justo lo que no se lee por la mañana.
        # Dentro caen cosas que se notan en la app sin explicación: "sin HIIT:
        # <motivo>", "progresión no aplicada: el semáforo está en amber", que la
        # rotación no se ha movido, o una regla especial que ha caducado y por
        # eso hoy vuelve un ejercicio que llevaba días fuera.
        # Las que ya se han dicho arriba se reconstruyen desde la MISMA fuente
        # que las produjo, no se reconocen por el texto. Buscar "descarga:" con
        # un `startswith` funcionaría hoy y dejaría de funcionar el día que
        # alguien reescriba la frase, sin avisar.
        ya_dicho = {f"descarga: {decision.deload.reason}"}
        apuntes = [
            n for n in list(s.notes) + list(decision.notes) if n not in ya_dicho
        ]
        for n in apuntes:
            L.append(f"• {escapar_html(n)}")

    texto = "\n".join(L)
    if len(texto) > LIMIT:
        # Se recorta por el final, que es donde está el razonamiento. Perder el
        # motivo es molesto; perder los ejercicios haría el mensaje inútil.
        corte = texto[: LIMIT - 40].rsplit("\n", 1)[0]
        texto = corte + "\n<i>[mensaje recortado]</i>"
    return texto


def render_plain(decision: Any, config: Any = None) -> str:
    """La misma información sin etiquetas HTML, para consola y logs.

    Usa el MISMO `sin_etiquetas` que el reintento en plano de `telegram.py`, y
    no una lista de etiquetas propia. La lista propia ya se había quedado corta:
    no conocía `<code>`, así que un mensaje con `<code>` salía por consola con
    las etiquetas a la vista. Y ahora además hay que deshacer el escapado, o el
    `--dry-run` imprimiría `&lt;` donde el mensaje real lleva un `<`.
    """
    return sin_etiquetas(render_telegram(decision, config))
