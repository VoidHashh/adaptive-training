"""CLI del motor. Su razón de ser es `--dry-run`.

    python -m app.cli --dry-run
    python -m app.cli --dry-run --date 2026-09-08 --checkin lower=2,fatigue=5
    python -m app.cli --dry-run --offline    # sin tocar Garmin
    python -m app.cli --checkin-help         # deslizadores válidos

`--dry-run` recorre EXACTAMENTE el mismo camino que la ejecución real —lee
Garmin, construye las señales, decide, arma la sesión, compone el mensaje y
construye el cuerpo del PUT de Hevy— y se detiene justo antes de escribir. Esa
es la única diferencia, y es deliberado: un ensayo que usa otro código no
ensaya nada.

EL ORIGEN DE LOS DATOS SE DICE SIEMPRE Y EN ALTO
------------------------------------------------
No basta con etiquetar "reales" y confiar. Una etiqueta fija miente en cuanto
algo va mal: si Garmin devuelve 429, o si un endpoint falla y los días vuelven
vacíos, un informe que siga diciendo "7 días reales" está afirmando algo que no
ha comprobado. Por eso la cabecera no declara la intención sino el RESULTADO:
cuántos días trajeron cada métrica, de dónde salen las salidas en bici y qué
incidencias hubo. Y los avisos se pintan con el mismo tamaño de letra que el
cartel de datos de ejemplo, porque un aviso discreto es un aviso que no se lee.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

from app.config_loader import load_config
from app.engine.decision import EngineState, decide
from app.engine.message import render_plain, render_telegram
from app.engine.signals import Checkin, DayMetrics, Ride, build_signals
from app.settings import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
ANCHO = 78

# Los días de histórico de SALIDAS ya no viven aquí. Había un
# `RIDE_HISTORY_DAYS = 190` con un comentario correcto -las actividades vienen
# en una sola petición por rango y los umbrales adaptativos necesitan 60 días
# de distribución- que aun así contradecía a `cycling.fetch` en el YAML. Ahora
# lo decide `activity_cache.ventana_de_salidas`, que además sabe distinguir
# entre "hay que rehacer el histórico" y "basta releer la última semana".

# Nombres cortos para el check-in en línea de órdenes, para no escribir
# `lower_discomfort=3` a las siete de la mañana. La clave de la derecha se
# valida contra `checkin_sliders` del YAML antes de usarse.
CHECKIN_ALIAS = {
    "fatigue": "fatigue",
    "fatiga": "fatigue",
    "mood": "mood",
    "animo": "mood",
    "upper": "upper_discomfort",
    "lower": "lower_discomfort",
    "sleep": "sleep_quality",
    "sleep_quality": "sleep_quality",
    "desire": "training_desire",
    "ganas": "training_desire",
    "rpe": "yesterday_rpe",
}


def checkin_help(cfg) -> str:
    """Documenta los deslizadores REALES del YAML, no una lista escrita a mano.

    Se genera desde la configuración porque una ayuda copiada a mano se queda
    obsoleta el día que se añade un deslizador, y entonces enseña a escribir
    check-ins que el motor ignora.
    """
    inverso: dict[str, list[str]] = {}
    for alias, clave in CHECKIN_ALIAS.items():
        inverso.setdefault(clave, []).append(alias)

    lineas = ["Deslizadores de --checkin en este config.yaml:", ""]
    for s in cfg.raw.get("checkin_sliders", []):
        clave = s["key"]
        alias = sorted(a for a in inverso.get(clave, []) if a != clave)
        corto = f"   [alias: {', '.join(alias)}]" if alias else ""
        lineas.append(f"  {clave:<18} {s.get('label', '')}{corto}")

    claves = [s["key"] for s in cfg.raw.get("checkin_sliders", [])]
    lineas += [
        "",
        "Todos los campos, nombres largos:",
        '  --checkin "' + ",".join(f"{k}=5" for k in claves) + '"',
        "",
        "Todos los campos, alias cortos:",
        '  --checkin "fatigue=4,mood=7,upper=1,lower=2,sleep=7,desire=8,rpe=6"',
        "",
        "Una clave que no esté en la lista es un error, no un valor ignorado.",
    ]
    return "\n".join(lineas)


def parse_checkin(text: str | None, day: date, valid_keys: set[str]) -> Checkin | None:
    """`lower=2,fatigue=5` -> Checkin.

    Una clave desconocida es un error duro. Antes se aceptaba cualquier cosa, y
    una errata (`fatige=5`) se traducía en que la regla correspondiente saliera
    como "sin datos para evaluar" sin que nadie pudiera sospechar por qué. Un
    check-in que se ignora en silencio es peor que uno que no se escribe.
    """
    if not text:
        return None
    values: dict[str, int | None] = {}
    for par in text.split(","):
        par = par.strip()
        if not par:
            continue
        if "=" not in par:
            raise SystemExit(f"check-in mal escrito: '{par}'. Formato: clave=valor")
        k, v = par.split("=", 1)
        k = k.strip()
        key = CHECKIN_ALIAS.get(k, k)
        if key not in valid_keys:
            raise SystemExit(
                f"\n  check-in: '{k}' no es un deslizador de este config.yaml.\n"
                f"  Válidos: {', '.join(sorted(valid_keys))}\n"
                f"  Alias:   {', '.join(sorted(CHECKIN_ALIAS))}\n"
            )
        try:
            values[key] = int(v)
        except ValueError:
            raise SystemExit(f"check-in: '{v}' no es un número entero") from None
    return Checkin(date=day, values=values)


# ---------------------------------------------------------------------------
# Procedencia de los datos
# ---------------------------------------------------------------------------


@dataclass
class Procedencia:
    """Qué datos se han usado de verdad. Medido, no supuesto."""

    titular: str
    detalle: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    es_ejemplo: bool = False

    def banner(self) -> list[str]:
        """Los avisos van con el mismo peso visual que el cartel de ejemplo."""
        if not self.avisos:
            return []
        out = ["", "!" * ANCHO]
        for a in self.avisos:
            out.append(f"  ATENCIÓN: {a}")
        out.append("!" * ANCHO)
        return out


def _completitud(
    metrics: list[DayMetrics], cfg: Any = None
) -> tuple[list[str], list[str]]:
    """Cuenta qué trajo realmente cada métrica. Devuelve (detalle, avisos).

    El aviso NO salta solo cuando una métrica viene a cero. Saltaba solo ahí, y
    ese era el problema: 4 de 7 días de HRV no avisaba de nada, pero la línea
    base necesita `min_days_required` días en la ventana, así que por debajo de
    ese número el `hrv_ratio` no existe y las reglas que lo usan no se evalúan.
    El sistema seguía dando su semáforo verde sin mencionar que le faltaba la
    mitad de la información con la que se supone que lo calcula.
    """
    n = len(metrics)
    campos = [
        ("hrv", "HRV", True),
        ("rhr", "FC reposo", True),
        ("sleep_min", "sueño", False),
        ("sleep_score", "score sueño", False),
        ("body_battery", "body battery", False),
    ]

    raw = (cfg.raw if hasattr(cfg, "raw") else cfg) or {}
    # Misma ruta que usa `build_signals`: `baseline` cuelga de la raíz, no de
    # `signals`. Leerlo de otro sitio daría el defecto de 4 para siempre y este
    # aviso mentiría sobre el umbral real.
    minimo = int((raw.get("baseline") or {}).get("min_days_required", 4))

    detalle: list[str] = []
    avisos: list[str] = []
    trozos = []
    for attr, etiqueta, tiene_base in campos:
        c = sum(1 for m in metrics if getattr(m, attr, None) is not None)
        trozos.append(f"{etiqueta} {c}/{n}")
        if not n:
            continue
        if c == 0:
            avisos.append(
                f"NINGÚN día trajo {etiqueta}. Las reglas que dependan de esa "
                f"señal no se han podido evaluar."
            )
        elif tiene_base and c < minimo:
            avisos.append(
                f"solo {c}/{n} días trajeron {etiqueta}, y la línea base "
                f"necesita {minimo}. Hay dato de hoy pero no hay contra qué "
                f"compararlo: las reglas que miran la desviación no se evalúan."
            )
        elif c < n:
            hueco = n - c
            verbo = "falta" if hueco == 1 else "faltan"
            dia = "día" if hueco == 1 else "días"
            avisos.append(
                f"{verbo} {hueco} de {n} {dia} de {etiqueta}. No invalida la "
                f"lectura, pero el histórico va con huecos."
            )
    detalle.append("wellness recibido: " + ", ".join(trozos))
    return detalle, avisos


def synthetic_window(day: date, days: int) -> tuple[list[DayMetrics], list[Ride]]:
    """Ventana de ejemplo. Existe para poder ensayar sin credenciales.

    Los valores son deliberadamente NORMALES: un perfil sin nada roto, para que
    lo que se vea en el ensayo sea el camino feliz y no un caso extremo. Si
    hicieran falta casos extremos, para eso están los tests.
    """
    metrics = []
    for i in range(days):
        d = day - timedelta(days=days - 1 - i)
        metrics.append(
            DayMetrics(
                date=d,
                hrv=[62, 58, 64, 61, 59, 63, 60][i % 7],
                rhr=[48, 49, 47, 48, 50, 48, 47][i % 7],
                sleep_min=[430, 405, 455, 420, 390, 445, 425][i % 7],
                sleep_score=[78, 71, 84, 76, 68, 81, 77][i % 7],
                body_battery=[72, 65, 80, 70, 61, 76, 74][i % 7],
            )
        )
    rides = [
        Ride(
            date=day - timedelta(days=2),
            duration_s=5400,
            distance_m=48000.0,
            zones=(900.0, 2400.0, 1500.0, 500.0, 100.0),
            aerobic_te=3.1,
            name="Salida de ejemplo (sábado)",
        ),
    ]
    return metrics, rides


def cargar_cache_salidas(usar: bool) -> tuple[list[Ride], list[str], list[str], Any]:
    """Histórico largo de salidas desde `data/cache/activities.json`.

    Devuelve también el objeto `CachedActivities` (o `None` con `--no-cache`)
    porque es lo que necesita `ventana_de_salidas` para decidir cuánto pedirle
    a Garmin: sin caché o con una caché corta hay que traer el histórico
    entero, y con una sana basta la ventana de `cycling.fetch.lookback_days`.
    """
    if not usar:
        return [], ["histórico largo desactivado (--no-cache)"], [
            "sin histórico largo, los umbrales adaptativos de carga no tienen "
            "base y `carga_acumulada` no se podrá evaluar"
        ], None

    from app.integrations.activity_cache import RUTA_CACHE_SALIDAS, load_cached_rides

    cache = load_cached_rides(RUTA_CACHE_SALIDAS)
    if not cache.available:
        return [], [], [
            f"sin histórico largo de salidas ({cache.describe()}); los umbrales "
            f"adaptativos de carga se quedarán sin base y `carga_acumulada` no "
            f"se podrá evaluar"
        ], cache
    detalle = [f"histórico de carga: {cache.describe()}"]
    if cache.file_mtime:
        detalle.append(f"                    caché escrita el "
                       f"{cache.file_mtime:%Y-%m-%d %H:%M}")
    return cache.rides, detalle, [], cache


def fetch_garmin(
    day: date, days: int, usar_cache: bool, cfg: Any = None
) -> tuple[list[DayMetrics], list[Ride], Procedencia]:
    """Datos reales del reloj, con el histórico largo desde la caché."""
    from app.integrations.activity_cache import merge_rides, ventana_de_salidas
    from app.integrations.garmin import GarminError, GarminRateLimited, build_client

    cached, det_cache, avisos_cache, cache = cargar_cache_salidas(usar_cache)
    ride_days, motivo_ventana = ventana_de_salidas(cfg, day, cache)
    det_cache = det_cache + [f"ventana de salidas: {motivo_ventana}"]

    try:
        client = build_client(settings)
        client.connect()
        metrics, rides = client.window(day, days, ride_days=ride_days)
    except GarminRateLimited as exc:
        raise SystemExit(
            f"\n{'!' * ANCHO}\n"
            f"  GARMIN LIMITÓ LA PETICIÓN (429). NO HAY DATOS FRESCOS.\n"
            f"{'!' * ANCHO}\n\n"
            f"  {exc}\n\n"
            f"  No se decide con datos a medias, así que no se ha decidido nada.\n"
            f"  Garmin limita por IP y el bloqueo se levanta solo: espera un rato\n"
            f"  y repite. Cada login nuevo empeora el bloqueo, así que no\n"
            f"  conviene insistir en bucle.\n\n"
            f"  Mientras tanto, `--offline` recorre el mismo camino con datos de\n"
            f"  ejemplo claramente marcados como tales.\n"
        ) from exc
    except GarminError as exc:
        raise SystemExit(
            f"\n  No se pudo leer Garmin: {exc}\n\n"
            f"  Para la prueba con datos reales hace falta un fichero .env en\n"
            f"  {REPO_ROOT} con:\n\n"
            f"      GARMIN_EMAIL=tu_correo\n"
            f"      GARMIN_PASSWORD=tu_contraseña\n\n"
            f"  Escríbelo tú: el sistema no pide credenciales por consola.\n"
            f"  Mientras tanto, `--offline` recorre el mismo camino con datos\n"
            f"  de ejemplo claramente marcados como tales.\n"
        ) from exc

    detalle, avisos = _completitud(metrics, cfg)
    detalle += det_cache
    avisos += avisos_cache

    frescas = len(rides)
    todas = merge_rides(cached, rides)
    detalle.append(
        f"salidas: {frescas} leídas ahora + {len(cached)} en caché "
        f"= {len(todas)} tras fusionar"
    )

    if client.session_resumed:
        detalle.append("sesión reanudada desde los tokens guardados (sin login)")
    if client.rate_limit_events:
        avisos.append(
            f"Garmin devolvió 429 {len(client.rate_limit_events)} vez/veces "
            f"durante esta lectura ({client.rate_limit_events[0]}). Se reintentó "
            f"y los datos que ves SÍ llegaron, pero la IP está limitada: repetir "
            f"la orden muchas veces empeora el bloqueo."
        )

    # Un hueco por error de lectura no es lo mismo que un hueco de verdad. Los
    # dos dejan la métrica en None, así que sin esto la única diferencia entre
    # "esa noche no hubo HRV" y "no se pudo leer el HRV de esa noche" era un
    # `log.debug` que el nivel por defecto ni imprime.
    if client.fetch_errors:
        avisos.append(
            f"{len(client.fetch_errors)} lectura(s) de wellness fallaron y el "
            f"hueco que dejan NO significa que no hubiera dato: "
            f"{client.fetch_errors[0]}"
            + (f" (+{len(client.fetch_errors) - 1} más)"
               if len(client.fetch_errors) > 1 else "")
        )

    return metrics, todas, Procedencia(
        titular=f"Garmin Connect — {days} días de wellness leídos ahora",
        detalle=detalle,
        avisos=avisos,
    )


# ---------------------------------------------------------------------------
# Informe
# ---------------------------------------------------------------------------


def imprimir_hevy(decision, cfg, mostrar_remoto: bool) -> None:
    """Qué se escribiría en Hevy. Es la parte con más riesgo del sistema."""
    from app.integrations.hevy import build_routine_payload, payload_diff

    print("-" * ANCHO)
    print("  LO QUE SE ESCRIBIRÍA EN HEVY (y no se escribe)")
    print("-" * ANCHO)

    s = decision.session
    hevy_cfg = (cfg.raw.get("integrations") or {}).get("hevy") or {}
    encendido = bool(hevy_cfg.get("write_enabled", False))
    print(f"  interruptor integrations.hevy.write_enabled = {encendido}")
    if not encendido:
        print("    -> MODO SOLO LECTURA. Aunque esto no fuera un ensayo, no se")
        print("       escribiría nada en Hevy. Enciéndelo cuando el mensaje de")
        print("       cada mañana coincida con lo que habrías hecho tú.")
    print()

    if not (s.write_to_hevy and s.routine_key):
        print("  Hoy no hay nada que escribir: la sesión no toca Hevy.")
        print()
        return

    rid = s.hevy_routine_id
    payload = build_routine_payload(s, cfg)
    rutina = payload["routine"]
    print(f"  PUT /v1/routines/{rid}")
    print(f"  título: {rutina['title']}   ({len(rutina['exercises'])} ejercicios)")
    print("  (PUT es REEMPLAZO TOTAL: esto no se suma a la rutina, la sustituye)")
    print()

    for ex in rutina["exercises"]:
        print(f"  [{ex['index']}] {ex['title']}"
              f"   ·   template {ex['exercise_template_id']}")
        if ex.get("superset_id") is not None:
            print(f"       superserie: {ex['superset_id']}")
        for st in ex["sets"]:
            trozos = []
            if st.get("reps") is not None:
                trozos.append(f"{st['reps']} reps")
            if st.get("duration_seconds") is not None:
                trozos.append(f"{st['duration_seconds']} s")
            if st.get("weight_kg") is not None:
                trozos.append(f"{st['weight_kg']} kg")
            if st.get("distance_meters") is not None:
                trozos.append(f"{st['distance_meters']} m")
            print(f"       serie {st['index'] + 1}: {st['type']:<7} "
                  + " · ".join(trozos))
        print()

    if mostrar_remoto and rid:
        print("  Comparación con lo que hay AHORA en Hevy:")
        try:
            from app.integrations.hevy import build_client as hevy_client

            remoto = hevy_client(settings, cfg).get_routine(rid)
            for linea in payload_diff(remoto, payload):
                print(f"    {linea}")
        except Exception as exc:  # noqa: BLE001 - leer Hevy aquí es opcional
            print(f"    no se pudo leer el estado remoto: {exc}")
            print("    (lectura opcional; no afecta a la decisión)")
        print()


def estado_para_el_ensayo(cfg) -> tuple[EngineState, str]:
    """El estado REAL de la base de datos, o uno en frío diciendo que lo es.

    El ensayo arrancaba siempre con `EngineState(program_start=...)` y el resto
    en cero. Lo declaraba en una línea del informe, pero la línea no arreglaba el
    problema: sin `active_rules` el ensayo enseña una sesión que no es la que
    saldría. Si una regla tiene el peso muerto retirado catorce días, el ensayo
    lo pinta igual, y quien lo lee se prepara para un entrenamiento que el
    sistema no va a mandar.

    Con las rachas pasa lo simétrico: en frío todas valen cero, así que las
    puertas de progresión salen cerradas y el ensayo predice DE MENOS. Un ensayo
    que se equivoca siempre en la misma dirección es el que peor se detecta,
    porque nunca sorprende.

    No escribe DATOS: se lee y se cierra. Un ensayo en seco que dejara rastro en
    el histórico sería peor que no tenerlo, y aquí basta con no llamar a
    `save_state`.

    Sí pone el ESQUEMA al día, y son cosas distintas. `ensure_schema` es lo
    mismo que hace la aplicación al arrancar, así que no ejecutarlo aquí no
    evita un efecto: lo aplaza, y mientras tanto hace que el ensayo prediga
    sobre una base de datos que no es la que va a existir dentro de un minuto.
    Eso ya pasó: una columna nueva en `models.py` que no estaba todavía en
    `data/app.db` tiraba la lectura entera y el ensayo salía EN FRÍO -sin
    reglas activas y con todas las rachas a cero-, es decir, prediciendo de
    menos, que es justo el error que este módulo dice dos párrafos más arriba
    que es el más difícil de detectar.

    Lo que sigue sin hacerse es CREAR la base: si no hay fichero, no se
    inventa uno por mirar.
    """
    frio = EngineState(program_start=cfg.program_start)
    ruta = str(settings.database_url).split("///")[-1]
    if "///" in str(settings.database_url) and not Path(ruta).is_file():
        return frio, f"sin base de datos en {ruta} (sin rachas ni reglas previas)"

    try:
        from app import repository as repo
        from app.db import SessionLocal, ensure_schema

        ensure_schema()
        with SessionLocal() as s:
            estado = repo.load_state(s, program_start=cfg.program_start)
    except Exception as exc:  # noqa: BLE001
        # Que no se pueda leer NO puede pasar por "no hay nada guardado": son
        # cosas distintas y llevan a informes distintos. Se sigue en frío, pero
        # diciéndolo.
        return frio, f"NO SE PUDO LEER la base de datos ({exc}); se sigue en frío"

    reglas = len(getattr(estado, "active_rules", []) or [])
    rachas = sum(1 for v in (getattr(estado, "clean_sessions", {}) or {}).values() if v)
    return estado, (
        f"leído de la base de datos · {reglas} regla(s) activa(s) · "
        f"{rachas} ejercicio(s) con racha"
    )


def sesiones_para_el_ensayo(cfg, day: date) -> tuple[list, str]:
    """Lo que se entrenó de verdad, para que el ensayo gaste el mismo presupuesto.

    Va aparte de `estado_para_el_ensayo` porque esto depende del DÍA y el estado
    no, pero el motivo de que exista es el mismo que el de aquella: si el ensayo
    no lee esto, `intensity_budget` recibe una lista vacía y el ensayo cree que
    queda margen para una salida intensa que en producción ya está gastado. Otra
    vez un error en una sola dirección -predecir de más-, que es el que nunca
    sorprende a nadie y por eso no se detecta.

    Cuando no se puede leer, se dice. Devolver [] callando sería indistinguible
    de una semana sin entrenar.

    Y pone el esquema al día por su cuenta, sin dar por hecho que alguien lo ha
    hecho antes. `estado_para_el_ensayo` también llama a `ensure_schema`, pero se
    llama DESPUÉS que esta función, así que apoyarse en aquella era apoyarse en
    el orden de dos líneas de `main`. Eso ya falló: al añadir las columnas de
    entrenamientos sueltos a `workout_log`, el primer ensayo sobre la base
    antigua cazaba el `OperationalError` aquí abajo y anunciaba el presupuesto de
    intensas a cero. Error en una sola dirección otra vez -predecir MÁS margen
    del que hay- y encima solo en la primera ejecución, que es cuando uno mira el
    informe para comprobar que el cambio ha ido bien. `ensure_schema` es
    idempotente, así que llamarla dos veces no cuesta nada y elimina la
    dependencia de orden.
    """
    ruta = str(settings.database_url).split("///")[-1]
    if "///" in str(settings.database_url) and not Path(ruta).is_file():
        return [], "sin base de datos (el presupuesto de intensas sale a cero)"
    try:
        from app import repository as repo
        from app.db import SessionLocal, ensure_schema

        ensure_schema()
        with SessionLocal() as s:
            sesiones = repo.sesiones_ejecutadas(
                s, cfg, desde=day - timedelta(days=14), hasta=day
            )
    except Exception as exc:  # noqa: BLE001
        return [], f"NO SE PUDO LEER lo entrenado ({exc}); presupuesto a cero"

    hiit = sum(1 for s in sesiones if s.is_hiit)
    return sesiones, (
        f"{len(sesiones)} sesión(es) en 14 días, {hiit} de ellas HIIT"
    )


def historial_para_el_ensayo(day: date) -> tuple[list, str]:
    """Los check-ins anteriores, para que el ensayo tenga la misma serie.

    Tercera vez el mismo párrafo, y no es pereza: el ensayo en seco es lo único
    que se mira antes de dejar que el sistema escriba solo, y cada dato que el
    ensayo NO lee es un sitio donde el ensayo dice una cosa y la mañana hace
    otra. `sig.history` alimenta los umbrales adaptativos; un ensayo sin
    histórico de formulario los calcularía sobre un punto y saldrían distintos
    de los de producción, que sí lo tiene.

    Hoy la diferencia es cero porque ningún umbral adaptativo mira un
    deslizador. Se conecta ahora precisamente por eso: conectarlo cuando ya
    importa significa descubrir el desajuste con las reglas nuevas puestas, y
    entonces no se sabe cuál de las dos cosas está mal.

    Devuelve [] con motivo cuando no se puede leer, igual que
    `sesiones_para_el_ensayo`, y por lo mismo: [] callando es indistinguible de
    "nunca has rellenado el formulario".
    """
    ruta = str(settings.database_url).split("///")[-1]
    if "///" in str(settings.database_url) and not Path(ruta).is_file():
        return [], "sin base de datos (sin serie para umbrales adaptativos)"
    try:
        from app import repository as repo
        from app.db import SessionLocal, ensure_schema

        ensure_schema()
        with SessionLocal() as s:
            historial = repo.historial_checkins(
                s, desde=day - timedelta(days=90), hasta=day - timedelta(days=1)
            )
    except Exception as exc:  # noqa: BLE001
        return [], f"NO SE PUDO LEER el histórico de check-ins ({exc}); serie vacía"

    if not historial:
        return [], "sin check-ins anteriores en 90 días"
    return historial, f"{len(historial)} check-in(s) en 90 días"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="adaptive",
        description="Motor de decisión de entrenamiento adaptativo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true",
                   help="decide y muestra, pero no escribe en Hevy ni envía Telegram")
    p.add_argument("--date", help="día a decidir (AAAA-MM-DD). Por defecto, hoy")
    p.add_argument("--checkin", help="check-in simulado: lower=2,fatigue=5,desire=7")
    p.add_argument("--checkin-help", action="store_true",
                   help="lista los deslizadores válidos y sale")
    p.add_argument("--offline", action="store_true",
                   help="no consultar Garmin; usar datos de ejemplo marcados")
    p.add_argument("--days", type=int, default=7, help="días de wellness (7)")
    p.add_argument("--no-cache", action="store_true",
                   help="no usar el histórico largo de data/cache/activities.json")
    p.add_argument("--hevy-diff", action="store_true",
                   help="leer la rutina actual de Hevy y comparar (solo lectura)")
    p.add_argument("--json", action="store_true", help="volcar la decisión en JSON")
    p.add_argument("--config", default=None, help="ruta a config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(Path(args.config) if args.config else settings.config_path)

    if args.checkin_help:
        print(checkin_help(cfg))
        return 0

    if not args.dry_run:
        print("Por ahora solo está implementado --dry-run.")
        return 2

    day = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    )

    if args.offline:
        metrics, rides = synthetic_window(day, args.days)
        cached, det_cache, avisos_cache, _cache = cargar_cache_salidas(not args.no_cache)
        if cached:
            from app.integrations.activity_cache import merge_rides

            rides = merge_rides(cached, rides)
        proc = Procedencia(
            titular=f"DATOS DE EJEMPLO ({args.days} días) — NO son tus datos reales",
            detalle=["wellness: inventado en synthetic_window()"] + det_cache,
            avisos=[
                "el wellness de este informe es INVENTADO. Sirve para ver el "
                "camino completo, no para decidir si entrenar hoy."
            ] + avisos_cache,
            es_ejemplo=True,
        )
    else:
        metrics, rides, proc = fetch_garmin(day, args.days, not args.no_cache, cfg)

    valid_keys = {s["key"] for s in cfg.raw.get("checkin_sliders", [])}
    checkin = parse_checkin(args.checkin, day, valid_keys)
    if checkin:
        faltan = sorted(valid_keys - set(checkin.values))
        origen_ci = f"simulado, {len(checkin.values)}/{len(valid_keys)} campos"
        if faltan:
            proc.detalle.append(
                f"check-in incompleto, sin: {', '.join(faltan)}"
            )
            proc.detalle.append(
                "                    (las reglas que dependan de esos campos no "
                "se evaluarán)"
            )
    else:
        origen_ci = "sin check-in"

    sesiones, origen_sesiones = sesiones_para_el_ensayo(cfg, day)
    historial, origen_historial = historial_para_el_ensayo(day)
    signals = build_signals(
        cfg,
        day,
        metrics=metrics,
        rides=rides,
        checkin=checkin,
        sessions=sesiones,
        checkin_history=historial,
    )

    state, origen_estado = estado_para_el_ensayo(cfg)
    decision = decide(cfg, day, signals, state, source="dry_run")

    print("=" * ANCHO)
    print("  ENSAYO EN SECO — no se escribe en Hevy ni se envía nada")
    print("=" * ANCHO)
    print(f"  fecha        : {day} ({decision.weekday})")
    print(f"  config       : {settings.config_path.name}  hash={cfg.hash}")
    print(f"  datos        : {proc.titular}")
    for linea in proc.detalle:
        print(f"                 {linea}")
    print(f"  check-in     : {origen_ci}")
    print(f"  programa     : inicio {cfg.program_start} · descarga "
          f"{'ACTIVA' if decision.deload.active else 'no'} "
          f"({decision.deload.reason})")
    print(f"  estado       : {origen_estado}")
    print(f"  entrenado    : {origen_sesiones}")
    print(f"  histórico CI : {origen_historial}")
    print("=" * ANCHO)
    for linea in proc.banner():
        print(linea)

    if args.json:
        print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print()
    print("-" * ANCHO)
    print("  MENSAJE DE TELEGRAM (tal cual se enviaría)")
    print("-" * ANCHO)
    print()
    print(render_plain(decision, cfg))
    print()

    print("-" * ANCHO)
    print("  TRAZA DE LA DECISIÓN")
    print("-" * ANCHO)
    print(f"  semáforo: {decision.light}"
          + (f" por '{decision.trigger_rule}'" if decision.trigger_rule else ""))
    for r in decision.light_decision.fired:
        print(f"    · disparó [{r.level}] {r.name}: {'; '.join(r.detail)}")
    saltadas = decision.light_decision.skipped
    if saltadas:
        print(f"  reglas sin evaluar por falta de datos: {len(saltadas)}")
        for r in saltadas:
            print(f"    · {r.name} (falta: {', '.join(sorted(set(r.missing)))})")
    for nombre, valor in sorted(signals.adaptive.items()):
        estado = f"{valor:.1f}" if valor is not None else "SIN BASE"
        print(f"  umbral adaptativo {nombre}: {estado}")
    print(f"  sesión: {decision.session.kind} — {decision.session.title}")
    for c in decision.session.changes:
        print(f"    · {c}")
    if decision.progression:
        pr = decision.progression
        print(f"  puerta general: {pr.gate_open} ({pr.gate_reason})")
        print(f"  puerta series : {pr.sets_allowed} ({pr.sets_reason})")
        print(f"  puerta reps   : {pr.reps_allowed} ({pr.reps_reason})")
    for n in decision.notes:
        print(f"  nota: {n}")

    print()
    imprimir_hevy(decision, cfg, args.hevy_diff)

    texto = render_telegram(decision, cfg)
    tg_cfg = (cfg.raw.get("integrations") or {}).get("telegram") or {}
    print("-" * ANCHO)
    print("  TELEGRAM (no enviado)")
    print("-" * ANCHO)
    print(f"  integrations.telegram.send_enabled = "
          f"{bool(tg_cfg.get('send_enabled', True))}")
    print(f"  {len(texto)} caracteres al chat configurado")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
