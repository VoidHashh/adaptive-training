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
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.engine.sets import warmup_flags

LIMIT = 4096  # límite duro de Telegram

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
EMOJI = {"green": "🟢", "amber": "🟡", "red": "🔴"}
NOMBRE_LUZ = {"green": "VERDE", "amber": "ÁMBAR", "red": "ROJO"}


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


def render_telegram(decision: Any, config: Any = None) -> str:
    """El mensaje completo del día."""
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    set_cfg = raw.get("set_types", {}) or {}
    notif = ((raw.get("notifications") or {}).get("telegram") or {})
    incluir_motivo = bool(notif.get("include_reasoning", True))

    L: list[str] = []
    luz = decision.light
    L.append(f"{EMOJI.get(luz, '⚪')} <b>{fmt_date(decision.day).capitalize()} — {NOMBRE_LUZ.get(luz, luz.upper())}</b>")

    if decision.deload.active:
        L.append("🔻 <i>Semana de descarga</i>")

    # --- la sesión ----------------------------------------------------------
    s = decision.session
    L.append("")
    if s.kind in {"rest", "pool", "bike"}:
        L.append(f"😴 <b>{s.title}</b>")
    else:
        etiqueta = {
            "full": "sesión completa",
            "reduced": "sesión reducida",
            "recovery": "recuperación",
        }.get(s.kind, s.kind)
        cab = f"💪 <b>{s.title}</b> ({etiqueta})"
        if s.deferred_from:
            cab += f"\n<i>Recuperas la sesión del {fmt_short(s.deferred_from)}</i>"
        L.append(cab)
        for ex in s.exercises:
            L.append(f"• {ex.get('name', ex.get('key'))} — {_describe_sets(ex, set_cfg)}")
        if s.hiit_block:
            L.append(f"🔥 <b>HIIT:</b> {s.hiit_block}")

    # --- lo que ha cambiado hoy --------------------------------------------
    cambios = decision.progression.changes if decision.progression else []
    if cambios:
        L.append("")
        L.append("📈 <b>Sube hoy</b>")
        for e in cambios:
            L.append(f"• {e.text()}")

    # Retiradas y recortes: son cambios que el usuario notará en la app y que
    # sin explicación parecen un fallo del sistema.
    if s.dropped:
        L.append("")
        L.append(f"➖ <b>Fuera hoy:</b> {', '.join(s.dropped)}")

    # --- reglas especiales vigentes ----------------------------------------
    reglas = [r for r in decision.active_rules if r.name != "semana_de_descarga"]
    if reglas:
        L.append("")
        L.append("⚠️ <b>Reglas activas</b>")
        for r in reglas:
            hasta = f" (hasta el {fmt_short(r.active_until)})" if r.active_until else ""
            L.append(f"• {r.name.replace('_', ' ').capitalize()}{hasta}")

    # --- bici ---------------------------------------------------------------
    if decision.bike is not None and decision.bike.applies:
        L.append("")
        L.append(f"🚴 {decision.bike.text()}")

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
            det = "; ".join(disparo.detail) if disparo and disparo.detail else ""
            L.append(f"• {decision.trigger_rule}{f': {det}' if det else ''}")
        else:
            L.append("• Ninguna regla ha saltado hoy")

        if decision.progression and not decision.progression.gate_open:
            L.append(f"• Progresión cerrada: {decision.progression.gate_reason}")

        # Las señales que faltan importan: una regla que no se pudo evaluar no
        # es una regla que no disparó, y conviene saber la diferencia.
        # Solo se nombran las reglas que no se pudieron evaluar, no cada señal
        # que faltaba. Las señales derivadas (hrv_ratio, hrv_baseline, ...)
        # multiplican la lista sin añadir información: si falta el HRV faltan
        # las tres, y lo accionable es "hoy no se pudo mirar el HRV".
        sin_datos = sorted({r.name for r in decision.light_decision.skipped})
        if sin_datos:
            cabe = sin_datos[:4]
            resto = len(sin_datos) - len(cabe)
            cola = f" (+{resto})" if resto > 0 else ""
            L.append(f"• Sin datos para evaluar: {', '.join(cabe)}{cola}")

    texto = "\n".join(L)
    if len(texto) > LIMIT:
        # Se recorta por el final, que es donde está el razonamiento. Perder el
        # motivo es molesto; perder los ejercicios haría el mensaje inútil.
        corte = texto[: LIMIT - 40].rsplit("\n", 1)[0]
        texto = corte + "\n<i>[mensaje recortado]</i>"
    return texto


def render_plain(decision: Any, config: Any = None) -> str:
    """La misma información sin etiquetas HTML, para consola y logs."""
    txt = render_telegram(decision, config)
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        txt = txt.replace(tag, "")
    return txt
