"""Reproduce el semáforo sobre el histórico ya guardado. NO decide nada.

Para qué sirve
--------------
El motor lleva meses sin correr: la tabla `decisions` está vacía y lo estará
hasta que el sistema arranque de verdad. Pero el backfill trajo seis meses de
wellness y de salidas, y con eso se puede preguntar una cosa que no se puede
preguntar de ninguna otra manera: *¿qué habría dicho el semáforo aquellos días?*

No es una recalibración ni un ensayo de nada. Es leer el pasado con las reglas
de hoy, que es la única forma de saber si esas reglas ven lo que tienen que ver
ANTES de confiarles las mañanas.

Qué NO hace, y es lo importante
-------------------------------
No escribe. No toca `decisions`, ni `rule_states`, ni el YAML. No llama a
`decide()` -que planificaría sesiones y progresiones sobre un estado que no
existía entonces- sino solo a `evaluate_light()`, que es la pieza pura que
contesta la pregunta. Cualquier cosa que este script imprima se puede volver a
imprimir mañana idéntica, porque no ha cambiado nada al mirarlo.

Cómo leer la salida
-------------------
`skipped` no es `not_fired`. Una regla saltada es una regla que NO se pudo
evaluar por falta de dato, y en un replay sin check-ins eso es la mitad del
reglamento. Se cuentan aparte a propósito: sumarlas a las que no dispararon
diría "todo en orden" donde lo que hay es "no se ha mirado".

Uso:
    python scripts/replay_semaforo.py [--desde YYYY-MM-DD] [--hasta YYYY-MM-DD]
    python scripts/replay_semaforo.py --corte 2026-05-01   # compara dos tramos
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select

from app.config_loader import load_config
from app.db import session_scope
from app.engine.rules import FIRED, NOT_APPLICABLE, SKIPPED, evaluate_light
from app.engine.signals import DayMetrics, Ride, build_signals
from app.engine.tendencia import DecisionDia, evaluar_tendencia
from app.models import Activity, DailyMetrics

ROOT = Path(__file__).resolve().parents[1]
ANCHO = 78


def leer_metricas(session) -> list[DayMetrics]:
    """Las filas de `daily_metrics` tal cual, sin rellenar ningún hueco.

    Un `None` aquí viaja hasta la regla y la salta. Sustituirlo por la media, o
    por el último valor conocido, convertiría "no se midió" en "se midió y salió
    normal", que es exactamente la mentira que este replay tiene que evitar.
    """
    filas = session.execute(select(DailyMetrics).order_by(DailyMetrics.date)).scalars().all()
    return [
        DayMetrics(
            date=f.date,
            hrv=f.hrv,
            rhr=f.rhr,
            sleep_min=f.sleep_min,
            sleep_score=f.sleep_score,
            body_battery=f.body_battery,
        )
        for f in filas
    ]


def leer_salidas(session) -> list[Ride]:
    """Las actividades, reconstruidas como las vería el motor esa mañana.

    Las zonas se pasan como tupla de cinco aunque alguna sea `None`: es lo que
    `classify_ride` espera, y es lo que permite que una salida sin reparto de
    zonas acabe en `desconocida` en vez de en `suave`.
    """
    filas = session.execute(select(Activity).order_by(Activity.date)).scalars().all()
    return [
        Ride(
            date=f.date,
            duration_s=f.duration_s,
            distance_m=f.distance_m,
            zones=(f.hr_zone_1_s, f.hr_zone_2_s, f.hr_zone_3_s, f.hr_zone_4_s, f.hr_zone_5_s),
            training_load=f.training_load,
            aerobic_te=f.aerobic_te,
            anaerobic_te=f.anaerobic_te,
            is_cycling=bool(f.is_cycling),
            activity_id=f.garmin_activity_id,
            name=f.name,
            elevation_gain_m=f.elevation_gain_m,
            moving_duration_s=f.moving_duration_s,
            avg_hr=f.avg_hr,
        )
        for f in filas
    ]


def replay(cfg, metricas, salidas, desde: date, hasta: date) -> list[dict]:
    """Un `evaluate_light` por día. Sin check-in: no había."""
    dias = []
    d = desde
    while d <= hasta:
        sig = build_signals(cfg, d, metrics=metricas, rides=salidas, checkin=None)
        luz = evaluate_light(cfg, sig)
        dias.append(
            {
                "dia": d,
                "luz": luz.light,
                "trigger": luz.trigger_rule,
                "disparadas": [r.name for r in luz.fired],
                "saltadas": [r.name for r in luz.skipped],
                "detalle": {r.name: r.detail for r in luz.fired},
                "hrv_ratio": sig.get("hrv_ratio"),
                "rhr_delta": sig.get("rhr_delta"),
                "sleep_min": sig.get("sleep_min"),
                "load_3d": sig.get("load_3d"),
                "load_3d_p90": sig.adaptive.get("load_3d_p90"),
            }
        )
        d += timedelta(days=1)
    return dias


def reparto(dias: list[dict]) -> Counter:
    return Counter(d["luz"] for d in dias)


def pinta_reparto(titulo: str, dias: list[dict]) -> None:
    c = reparto(dias)
    n = len(dias) or 1
    print(f"  {titulo}  (n={len(dias)})")
    for luz in ("green", "amber", "red"):
        v = c.get(luz, 0)
        barra = "#" * int(round(40 * v / n))
        print(f"    {luz:6s} {v:4d}  {100*v/n:5.1f}%  {barra}")


def pinta_reglas(titulo: str, dias: list[dict]) -> None:
    disp = Counter(r for d in dias for r in d["disparadas"])
    trig = Counter(d["trigger"] for d in dias if d["trigger"])
    salt = Counter(r for d in dias for r in d["saltadas"])
    print(f"\n  {titulo}")
    print(f"    {'regla':28s} {'dispara':>8s} {'determin.':>10s} {'saltada':>8s}")
    print(f"    {'-'*28} {'-'*8} {'-'*10} {'-'*8}")
    for nombre in sorted(set(disp) | set(trig) | set(salt)):
        print(f"    {nombre:28s} {disp.get(nombre,0):8d} {trig.get(nombre,0):10d} "
              f"{salt.get(nombre,0):8d}")


def pinta_tendencia(cfg, dias: list[dict], metricas: list[DayMetrics]) -> None:
    """Lo que la capa de tendencia habría dicho cada mañana, día a día.

    Para qué sirve exactamente
    --------------------------
    Los cinco umbrales de `trend` son ABSOLUTOS, no adaptativos, y eso está
    razonado en el YAML. Pero un umbral absoluto que nadie contrasta contra los
    datos es un número inventado con buena prosa alrededor. Esto es lo que lo
    contrasta: si `racha_min: 5` dispara ochenta veces en seis meses, sobra; si
    no dispara ninguna, no mide nada. Las dos cosas se ven aquí y en ningún otro
    sitio.

    Se alimenta con las decisiones que el motor HABRÍA tomado -no existen en
    `decisions`, que sigue vacía- y por eso la capa se diseñó pura y recibiendo
    una lista de `DecisionDia` en vez de abrir la base de datos ella misma.

    Los días consecutivos con la MISMA salida se agrupan en un rango. No es un
    resumen: no se pierde ni un día, y el rango dice exactamente cuáles. Sin
    agrupar, 179 líneas de "sin novedad" enterrarían las que cuentan algo.

    Qué cuenta como "la misma salida"
    ---------------------------------
    Los avisos se comparan por su TEXTO ENTERO, porque ahí el número es la
    noticia: "5 días seguidos" y "6 días seguidos" son dos cosas distintas y
    juntarlas sería mentir. Los N/A, en cambio, se comparan solo por TIPO,
    porque su texto lleva el contador del calentamiento ("18/30 días...") y
    cambia cada mañana sin que cambie nada: comparándolos por texto, los
    primeros cuarenta y pico días imprimían un bloque por día y no se agrupaba
    nada. El bloque se pinta con las líneas del ÚLTIMO día del rango, que son
    las que dicen cuánta muestra había al terminar de esperar.
    """
    score = {m.date: m.sleep_score for m in metricas}
    minutos = {m.date: m.sleep_min for m in metricas}

    historico: list[DecisionDia] = []
    salidas: list[tuple[date, tuple, list[str]]] = []
    cuenta: Counter = Counter()
    sin_muestra: Counter = Counter()

    for d in dias:
        historico.append(DecisionDia(d["dia"], d["luz"], d["trigger"]))
        t = evaluar_tendencia(
            cfg, d["dia"], historico, sleep_score=score, sleep_min=minutos
        )
        for a in t.avisos:
            cuenta[a.tipo] += 1
        for s in t.sin_muestra:
            sin_muestra[s.tipo] += 1
        clave = (
            tuple(a.texto for a in t.avisos),
            tuple(s.tipo for s in t.sin_muestra),
        )
        salidas.append((d["dia"], clave, t.lineas()))

    print("\n" + "=" * ANCHO)
    print("  CAPA DE TENDENCIA — lo que habría dicho cada mañana")
    print("=" * ANCHO)
    tr = (cfg.raw.get("trend") or {})
    print(f"  umbrales  : racha≥{tr.get('racha_min')}d · "
          f"{tr.get('ventana_corta_dias')}d vs {tr.get('ventana_larga_dias')}d "
          f"Δ≥{tr.get('delta_pp_min')}pp · motivo≥{tr.get('motivo_semanas_min')} sem")
    print(f"  disparos  : " + (", ".join(f"{k}×{v}" for k, v in cuenta.most_common())
                               or "NINGUNO — estos umbrales no miden nada"))
    print(f"  sin muestra: " + (", ".join(f"{k}×{v}" for k, v in sin_muestra.most_common())
                                or "—"))
    print()

    # Agrupa días consecutivos con salida idéntica.
    bloque_ini: date | None = None
    bloque_fin: date | None = None
    bloque_clave: tuple | None = None
    bloque_txt: list[str] | None = None

    def vuelca() -> None:
        if bloque_txt is None:
            return
        if bloque_ini == bloque_fin:
            cab = f"  {bloque_ini}"
        else:
            n = (bloque_fin - bloque_ini).days + 1
            cab = f"  {bloque_ini} → {bloque_fin} ({n}d)"
        print(f"{cab}")
        for linea in bloque_txt:
            print(f"      {linea}")

    for dia, clave, lineas in salidas:
        if bloque_clave == clave and bloque_fin == dia - timedelta(days=1):
            bloque_fin = dia
            bloque_txt = lineas
            continue
        vuelca()
        bloque_ini = bloque_fin = dia
        bloque_clave = clave
        bloque_txt = lineas
    vuelca()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desde", type=date.fromisoformat, default=None)
    p.add_argument("--hasta", type=date.fromisoformat, default=None)
    p.add_argument("--corte", type=date.fromisoformat, default=None,
                   help="compara el tramo anterior al corte con el posterior")
    p.add_argument("--mensual", action="store_true", help="reparto mes a mes")
    p.add_argument("--tendencia", action="store_true",
                   help="lo que la capa de tendencia habría dicho cada mañana")
    p.add_argument("--listar", choices=["amber", "red", "todos"], default=None)
    args = p.parse_args()

    cfg = load_config(ROOT / "config.yaml")

    with session_scope() as s:
        metricas = leer_metricas(s)
        salidas = leer_salidas(s)

    if not metricas:
        print("no hay ni una fila en daily_metrics: nada que reproducir")
        return 1

    desde = args.desde or metricas[0].date
    hasta = args.hasta or metricas[-1].date

    print("=" * ANCHO)
    print("  REPLAY DEL SEMÁFORO — solo lectura, no se escribe nada")
    print("=" * ANCHO)
    print(f"  config    : config.yaml  hash={cfg.hash}")
    print(f"  ventana   : {desde} → {hasta}  ({(hasta-desde).days + 1} días)")
    print(f"  wellness  : {len(metricas)} filas · salidas: {len(salidas)}")
    print("  check-ins : NINGUNO — las reglas subjetivas se saltan, no se asumen")
    print()

    dias = replay(cfg, metricas, salidas, desde, hasta)

    pinta_reparto("TODA LA VENTANA", dias)
    pinta_reglas("REGLAS", dias)

    if args.mensual:
        print("\n  MES A MES")
        por_mes: dict[str, list[dict]] = defaultdict(list)
        for d in dias:
            por_mes[d["dia"].strftime("%Y-%m")].append(d)
        print(f"    {'mes':9s} {'n':>4s} {'verde':>7s} {'ámbar':>7s} {'rojo':>6s}   reglas")
        for mes, sub in sorted(por_mes.items()):
            c = reparto(sub)
            reglas = Counter(r for d in sub for r in d["disparadas"])
            txt = ", ".join(f"{k}×{v}" for k, v in reglas.most_common()) or "—"
            print(f"    {mes:9s} {len(sub):4d} {c.get('green',0):7d} "
                  f"{c.get('amber',0):7d} {c.get('red',0):6d}   {txt}")

    if args.tendencia:
        pinta_tendencia(cfg, dias, metricas)

    if args.corte:
        antes = [d for d in dias if d["dia"] < args.corte]
        despues = [d for d in dias if d["dia"] >= args.corte]
        print("\n" + "=" * ANCHO)
        print(f"  COMPARATIVA — corte en {args.corte}")
        print("=" * ANCHO)
        pinta_reparto(f"ANTES  ({desde} → {args.corte - timedelta(days=1)})", antes)
        print()
        pinta_reparto(f"DESPUÉS ({args.corte} → {hasta})", despues)
        pinta_reglas("REGLAS ANTES", antes)
        pinta_reglas("REGLAS DESPUÉS", despues)

    if args.listar:
        print("\n" + "=" * ANCHO)
        print(f"  DÍAS ({args.listar})")
        print("=" * ANCHO)
        for d in dias:
            if args.listar != "todos" and d["luz"] != args.listar:
                continue
            det = "; ".join(f"{k}: {', '.join(v)}" for k, v in d["detalle"].items())
            print(f"  {d['dia']} {d['luz']:6s} {d['trigger'] or '—':24s} {det}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
