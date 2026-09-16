"""Mide si cada regla del semáforo distingue algo, o si es azar con nombre.

De dónde sale esto
------------------
Es el examen que mató a `carga_acumulada` y a `resaca_finde`. Las dos lápidas
están en `config.yaml` y llevan sus números; este script es el que los produce,
escrito para poder repetirlo sobre cualquier regla en vez de a mano una vez.

La pregunta NO es "¿qué umbral?". Es "¿debe existir?". Un umbral se ajusta
cuando la regla ya ha demostrado que ve algo; si no ve nada, moverlo es elegir
mejor el ruido.

Cómo se contesta
----------------
Se relee el histórico mañana a mañana con el motor de verdad -`evaluate_light`,
la misma pieza que decide- y para cada regla se pregunta: los días que dispara,
¿estaba el cuerpo peor que un día cualquiera?

El "cuerpo peor" es `hrv_ratio < 0,90`, el mismo criterio de las dos lápidas.
No es perfecto y por eso está escrito aquí: es un canal distinto del que usan
casi todas las reglas, tiene dato los 181 días, y ya se usó para condenar a dos
reglas, así que cambiarlo ahora sería mover la portería.

El test es una BINOMIAL DE UNA COLA contra la tasa de fondo. Reproduce los tres
p de la lápida de `carga_acumulada` hasta el último decimal impreso (0,0006 /
0,19 / 0,36), que es como se ha confirmado que era este y no la hipergeométrica
-que da 0,0003 y no cuadra-. Con corrección de Benjamini-Hochberg al final,
porque preguntar doce veces y quedarse con el mejor p es hacer trampas.

La circularidad, que es la trampa de verdad
-------------------------------------------
`hrv_baja_1d` ES `hrv_ratio < 0,90`. Medirla contra `hrv_ratio < 0,90` da 100 %
de aciertos y p ridículo POR CONSTRUCCIÓN, no por acierto. Lo mismo, algo menos
descarado, con `hrv_hundida_2d`, que es un subconjunto.

Esas dos se marcan CIRCULAR y no se les imprime p. Imprimirlo sería fabricar la
prueba más contundente de la tabla y que fuese exactamente la que no vale. Para
las que miden otro canal -pulso de reposo, sueño- el test sí dice algo.

Uso:
    python scripts/medir_reglas.py
    python scripts/medir_reglas.py --regla hrv_baja_1d --senal hrv_ratio --barrido 0.85,0.90,0.95
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.analysis.stats import benjamini_hochberg, percentil
from app.config_loader import load_config
from app.db import session_scope
from app.engine.rules import FIRED, SKIPPED, evaluate_rule
from app.engine.signals import build_signals

from replay_semaforo import leer_metricas, leer_salidas  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ANCHO = 92

# El listón de "el cuerpo estaba de verdad bajo". Ver el docstring: es el de las
# dos lápidas, y se deja fijo justamente para que no se pueda elegir a posteriori
# el que más favorezca a la regla que se está juzgando.
CUERPO_BAJO = ("hrv_ratio", 0.90)

# Reglas cuya condición es el criterio de resultado, o casi. No se les calcula p.
CIRCULARES = {"hrv_baja_1d": "es literalmente hrv_ratio < 0,90",
              "hrv_hundida_2d": "es hrv_ratio < 0,85 dos días: subconjunto del criterio"}


def binomial_cola(x: int, n: int, p: float) -> float:
    """P(X >= x) con n intentos y probabilidad p. La cola de arriba, exacta.

    Sin dependencias: `comb` es exacto con enteros y n aquí nunca pasa de 200,
    así que no hay ni desbordamiento ni pérdida que importe.
    """
    if n <= 0:
        return 1.0
    x = max(x, 0)
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(x, n + 1))


def evaluar_dia(cfg, sig) -> dict:
    """Todas las reglas de un día, con su estado. No para en la primera.

    `evaluate_light` devuelve la luz y el disparo, pero para medir hace falta
    saber TAMBIÉN el nivel de cada regla que disparó, porque "determinante" se
    calcula quitando la regla y volviendo a mirar de qué color queda el día.
    """
    raw = cfg.raw if hasattr(cfg, "raw") else cfg
    umbrales = raw.get("thresholds", {}) or {}
    disparadas: dict[str, str] = {}
    saltadas: list[str] = []
    for nivel in ("red", "amber"):
        for regla in umbrales.get(nivel, []) or []:
            res = evaluate_rule(regla, sig, level=nivel, options=raw)
            if res.status == FIRED:
                disparadas[res.name] = nivel
            elif res.status == SKIPPED:
                saltadas.append(res.name)
    return {"disparadas": disparadas, "saltadas": saltadas}


def color(disparadas: dict[str, str], excluir: str | None = None) -> str:
    niveles = {n for r, n in disparadas.items() if r != excluir}
    if "red" in niveles:
        return "red"
    if "amber" in niveles:
        return "amber"
    return "green"


def catalogo(cfg) -> dict[str, str]:
    raw = cfg.raw if hasattr(cfg, "raw") else cfg
    umbrales = raw.get("thresholds", {}) or {}
    return {
        r["name"]: nivel
        for nivel in ("red", "amber")
        for r in (umbrales.get(nivel, []) or [])
    }


def construir(cfg, metricas, salidas, desde: date, hasta: date) -> list[dict]:
    """Un día por fila, con sus señales y el veredicto de todas las reglas.

    `sessions` y `checkin_history` van vacíos porque en el pasado que se relee no
    hay ni sesiones de fuerza ni check-ins. Es el dato verdadero, no un hueco, y
    de ahí sale que media tabla salga SIN DATOS en vez de salir en cero.
    """
    dias = []
    d = desde
    while d <= hasta:
        sig = build_signals(
            cfg, d, metrics=metricas, rides=salidas,
            sessions=[], checkin_history=[], checkin=None,
        )
        v = evaluar_dia(cfg, sig)
        dias.append({
            "dia": d,
            "disparadas": v["disparadas"],
            "saltadas": v["saltadas"],
            "luz": color(v["disparadas"]),
            "hrv_ratio": sig.get("hrv_ratio"),
            "rhr_delta": sig.get("rhr_delta"),
            "sleep_min": sig.get("sleep_min"),
            # `sleep_score` no dispara ninguna regla del semáforo -lo dice el
            # comentario de `config.yaml`-, pero se arrastra igual porque la
            # pregunta "¿y si la regla mirase la nota en vez de los minutos?"
            # no se puede contestar sin el dato delante.
            "sleep_score": sig.get("sleep_score"),
        })
        d += timedelta(days=1)
    return dias


def cuerpo_bajo(dia: dict) -> bool | None:
    señal, liston = CUERPO_BAJO
    v = dia.get(señal)
    return None if v is None else v < liston


def tasa_de_fondo(dias: list[dict]) -> tuple[float, int, int]:
    medibles = [d for d in dias if cuerpo_bajo(d) is not None]
    bajos = sum(1 for d in medibles if cuerpo_bajo(d))
    return (bajos / len(medibles) if medibles else 0.0), bajos, len(medibles)


def medir_regla(nombre: str, dias: list[dict], fondo: float) -> dict:
    """Los cinco números de una regla, más el p si no es circular."""
    dispara = [d for d in dias if nombre in d["disparadas"]]
    salta = sum(1 for d in dias if nombre in d["saltadas"])
    # Determinante = sin ella el día habría sido de otro color. Es la pregunta
    # que importa: una regla que solo dispara cuando otra ya ha pintado el día
    # no está cambiando ni una sesión.
    determina = [d for d in dispara if color(d["disparadas"], excluir=nombre) != d["luz"]]
    medibles = [d for d in dispara if cuerpo_bajo(d) is not None]
    aciertos = [d for d in medibles if cuerpo_bajo(d)]
    # "Sola" = el cuerpo NO la acompañaba ese día.
    sola = [d for d in medibles if not cuerpo_bajo(d)]
    p = None
    if nombre not in CIRCULARES and medibles:
        p = binomial_cola(len(aciertos), len(medibles), fondo)
    return {
        "regla": nombre,
        "dispara": len(dispara),
        "determina": len(determina),
        "salta": salta,
        "n": len(medibles),
        "aciertos": len(aciertos),
        "sola": len(sola),
        "tasa": (len(aciertos) / len(medibles)) if medibles else None,
        "p": p,
        "circular": nombre in CIRCULARES,
        "dias_determina": determina,
    }


def pinta_tabla(filas: list[dict], niveles: dict[str, str], fondo: float, n_dias: int) -> None:
    ps = [f["p"] for f in filas]
    for f, q in zip(filas, benjamini_hochberg(ps)):
        f["q"] = q

    print("=" * ANCHO)
    print("  LAS REGLAS DEL SEMÁFORO, UNA A UNA")
    print("=" * ANCHO)
    print(f"  criterio de «cuerpo bajo»: {CUERPO_BAJO[0]} < {CUERPO_BAJO[1]}  "
          f"· tasa de fondo {100*fondo:.1f} %")
    print(f"  test: binomial de una cola contra esa tasa · q = Benjamini-Hochberg")
    print()
    cab = (f"  {'regla':22s} {'niv':>5s} {'disp':>5s} {'det':>4s} {'salta':>6s} "
           f"{'cuerpo':>9s} {'sola':>5s} {'p':>9s} {'q':>9s}  veredicto")
    print(cab)
    print("  " + "-" * (ANCHO - 2))
    for f in filas:
        n = niveles.get(f["regla"], "?")[:5]
        if f["dispara"] == 0 and f["salta"] >= n_dias:
            ver = "SIN DATOS — no se pudo mirar"
            cuerpo = p = q = "—"
        elif f["dispara"] == 0:
            ver = "NUNCA DISPARA en 181 días"
            cuerpo = p = q = "—"
        elif f["circular"]:
            ver = f"CIRCULAR — {CIRCULARES[f['regla']]}"
            cuerpo = f"{f['aciertos']}/{f['n']}"
            p = q = "n/a"
        else:
            cuerpo = f"{f['aciertos']}/{f['n']}"
            p = f"{f['p']:.4f}" if f["p"] is not None else "—"
            q = f"{f['q']:.4f}" if f.get("q") is not None else "—"
            if f["q"] is not None and f["q"] < 0.05:
                ver = "distingue"
            else:
                ver = "NO se distingue del azar"
        print(f"  {f['regla']:22s} {n:>5s} {f['dispara']:5d} {f['determina']:4d} "
              f"{f['salta']:6d} {cuerpo:>9s} {f['sola']:5d} {p:>9s} {q:>9s}  {ver}")


# De qué lado está lo malo. `rhr_delta` alto es un pulso disparado; `sleep_min`
# bajo es una mala noche. Barrer las dos con `>= corte` mide, en el caso del
# sueño, exactamente el grupo contrario al que la regla castiga.
SENTIDO = {"rhr_delta": "alto", "hrv_ratio": "bajo",
           "sleep_min": "bajo", "sleep_score": "bajo"}


def sentido_de(señal: str) -> str:
    return SENTIDO.get(señal, "alto")


def dispara_en(dia: dict, señal: str, corte: float, sentido: str) -> bool:
    v = dia.get(señal)
    if v is None:
        return False
    return v >= corte if sentido == "alto" else v < corte


def pinta_distribucion(dias: list[dict], señal: str, cortes: list[float]) -> None:
    vals = sorted(d[señal] for d in dias if d.get(señal) is not None)
    if not vals:
        print(f"\n  {señal}: sin un solo valor")
        return
    n = len(vals)
    media = sum(vals) / n
    print("\n" + "=" * ANCHO)
    print(f"  DISTRIBUCIÓN REAL DE {señal.upper()}  (n={n})")
    print("=" * ANCHO)
    print(f"  media {media:+.2f}   min {vals[0]:+.2f}   max {vals[-1]:+.2f}")
    pcts = [50, 75, 80, 90, 95, 97.5, 99]
    # `percentil` toma q en 0..100, no en 0..1. Pasarlo en tanto por uno no
    # revienta: devuelve el percentil 0,5 en vez del 50 y sale una tabla de
    # percentiles casi planos, que es justo lo que pasó la primera vez.
    print("  percentiles: " + "  ".join(
        f"p{p:g}={percentil(vals, p):+.2f}" for p in pcts))
    print()
    sentido = sentido_de(señal)
    signo = ">=" if sentido == "alto" else "<"
    print(f"    {'corte':>8s} {'días ' + signo:>9s} {'% de los días':>15s}")
    for c in cortes:
        k = sum(1 for v in vals if (v >= c if sentido == "alto" else v < c))
        print(f"    {c:>8.1f} {k:>9d} {100*k/n:>14.1f}%")


def pinta_barrido(cfg, dias: list[dict], señal: str, cortes: list[float],
                  fondo: float) -> None:
    """El mismo test a cada umbral candidato. Es el que decide si subirlo."""
    sentido = sentido_de(señal)
    print("\n" + "=" * ANCHO)
    print(f"  BARRIDO DE UMBRAL SOBRE {señal} — ¿alguno distingue algo?")
    print("=" * ANCHO)
    print(f"  dispara con {señal} {'>=' if sentido == 'alto' else '<'} umbral")
    print(f"    {'umbral':>8s} {'dispara':>8s} {'cuerpo bajo':>13s} {'tasa':>7s} "
          f"{'p':>9s} {'q':>9s}")
    crudos = []
    for c in cortes:
        dd = [d for d in dias if dispara_en(d, señal, c, sentido)
              and cuerpo_bajo(d) is not None]
        ac = sum(1 for d in dd if cuerpo_bajo(d))
        p = binomial_cola(ac, len(dd), fondo) if dd else None
        crudos.append({"c": c, "n": len(dd), "ac": ac, "p": p})
    for f, q in zip(crudos, benjamini_hochberg([x["p"] for x in crudos])):
        f["q"] = q
    for f in crudos:
        tasa = f"{100*f['ac']/f['n']:.0f}%" if f["n"] else "—"
        p = f"{f['p']:.4f}" if f["p"] is not None else "—"
        q = f"{f['q']:.4f}" if f["q"] is not None else "—"
        print(f"    {f['c']:>8.1f} {f['n']:>8d} {f['ac']:>13d} {tasa:>7s} "
              f"{p:>9s} {q:>9s}")

    # LA PRUEBA QUE MATÓ A `carga_acumulada`. Una regla puede acertar mucho y no
    # servir para nada: si los días que coge ya los cogía otra, no cambia ni una
    # sesión. Lo que cuenta es lo que aporta POR SU CUENTA, y si eso son días con
    # el cuerpo bien, lo que aporta son falsas alarmas.
    print("\n" + "=" * ANCHO)
    print(f"  LO QUE APORTA POR SU CUENTA — días que serían VERDES sin ella")
    print("=" * ANCHO)
    print(f"    {'umbral':>8s} {'propios':>8s} {'con el cuerpo bajo':>20s} "
          f"{'falsas alarmas':>16s}")
    for c in cortes:
        propios = [d for d in dias
                   if dispara_en(d, señal, c, sentido)
                   and color(d["disparadas"], excluir=nombre_regla(señal)) == "green"]
        bajos = [d for d in propios if cuerpo_bajo(d)]
        falsas = [d for d in propios if cuerpo_bajo(d) is False]
        print(f"    {c:>8.1f} {len(propios):>8d} {len(bajos):>20d} {len(falsas):>16d}")
        for d in falsas:
            hrv = f"{d['hrv_ratio']:.3f}" if d["hrv_ratio"] is not None else "—"
            print(f"             {d['dia']}  {señal} {d[señal]:+.2f}  "
                  f"hrv_ratio {hrv}  (el cuerpo decía que no)")


def nombre_regla(señal: str) -> str:
    """La regla cuya condición es esa señal, para poder excluirla del color."""
    return {"rhr_delta": "fc_reposo_elevada",
            "hrv_ratio": "hrv_baja_1d",
            "sleep_min": "sueno_corto"}.get(señal, "")


def pinta_dias(titulo: str, dias: list[dict], nombre: str) -> None:
    if not dias:
        return
    print(f"\n  {titulo}")
    for d in dias:
        hrv = f"{d['hrv_ratio']:.3f}" if d["hrv_ratio"] is not None else "  —  "
        rhr = f"{d['rhr_delta']:+.1f}" if d["rhr_delta"] is not None else "  — "
        slp = f"{d['sleep_min']:.0f}" if d["sleep_min"] is not None else " — "
        otras = ", ".join(sorted(r for r in d["disparadas"] if r != nombre)) or "ninguna"
        marca = "  <-- el cuerpo NO la acompañaba" if cuerpo_bajo(d) is False else ""
        print(f"    {d['dia']}  hrv_ratio {hrv}  rhr_delta {rhr}  sueño {slp}min  "
              f"otras: {otras}{marca}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    # `sueno_corto` y no `fc_reposo_elevada`: esa ya no existe -ver su lápida en
    # `config.yaml`- y un valor por defecto que apunta a una regla borrada haría
    # que el guion abriese siempre con "no existe ninguna regla llamada así".
    p.add_argument("--regla", default="sueno_corto",
                   help="la regla que se mira en detalle")
    p.add_argument("--señal", "--senal", dest="senal", default="sleep_min")
    p.add_argument("--barrido", default="330,360,390,420,450")
    args = p.parse_args()

    cortes = [float(x) for x in args.barrido.split(",") if x.strip()]
    cfg = load_config(ROOT / "config.yaml")

    with session_scope() as s:
        metricas = leer_metricas(s)
        salidas = leer_salidas(s)

    if not metricas:
        print("no hay ni una fila en daily_metrics: nada que medir")
        return 1

    desde, hasta = metricas[0].date, metricas[-1].date
    dias = construir(cfg, metricas, salidas, desde, hasta)
    fondo, bajos, medibles = tasa_de_fondo(dias)
    niveles = catalogo(cfg)

    print("=" * ANCHO)
    print("  MEDICIÓN DE LAS REGLAS — solo lectura, no se escribe nada")
    print("=" * ANCHO)
    print(f"  config   : hash={cfg.hash}")
    print(f"  ventana  : {desde} → {hasta}  ({len(dias)} días)")
    print(f"  wellness : {len(metricas)} filas · salidas: {len(salidas)}")
    print(f"  check-ins: NINGUNO — las reglas subjetivas se saltan, no se asumen")
    print(f"  fondo    : {bajos}/{medibles} días con {CUERPO_BAJO[0]} < {CUERPO_BAJO[1]} "
          f"= {100*fondo:.1f} %")
    print()

    reparto = Counter(d["luz"] for d in dias)
    print("  el semáforo sobre esos días: " + " · ".join(
        f"{k} {reparto.get(k,0)} ({100*reparto.get(k,0)/len(dias):.0f}%)"
        for k in ("green", "amber", "red")))
    print()

    filas = [medir_regla(n, dias, fondo) for n in niveles]
    filas.sort(key=lambda f: (-f["dispara"], f["regla"]))
    pinta_tabla(filas, niveles, fondo, len(dias))

    print("\n" + "=" * ANCHO)
    print(f"  EN DETALLE: {args.regla}")
    print("=" * ANCHO)
    detalle = next((f for f in filas if f["regla"] == args.regla), None)
    if detalle is None:
        print(f"  no existe ninguna regla llamada {args.regla}")
    else:
        print(f"  dispara {detalle['dispara']} veces · determina el color en "
              f"{detalle['determina']} · se salta {detalle['salta']}")
        if detalle["n"]:
            print(f"  el cuerpo la acompañaba en {detalle['aciertos']}/{detalle['n']} "
                  f"({100*detalle['tasa']:.0f} %) contra un fondo de {100*fondo:.1f} %")
            print(f"  iba SOLA en {detalle['sola']} de esos {detalle['n']} días")
        todos = [d for d in dias if args.regla in d["disparadas"]]
        acomp = sum(1 for d in todos
                    if any(r != args.regla for r in d["disparadas"]))
        print(f"  otra regla disparaba a la vez en {acomp} de {len(todos)}; "
              f"iba sola -sin ninguna otra- en {len(todos)-acomp}")
        pinta_dias(f"los {len(todos)} días en que disparó:", todos, args.regla)
        pinta_dias(f"los {len(detalle['dias_determina'])} días en que ELLA pintó el color:",
                   detalle["dias_determina"], args.regla)

    pinta_distribucion(dias, args.senal, cortes)
    pinta_barrido(cfg, dias, args.senal, cortes, fondo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
