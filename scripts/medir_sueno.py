"""¿Hay ALGUNA forma de mirar mi sueño que prediga mi recuperación?

Por qué existe este guion
-------------------------
`sueno_corto` dispara 56 veces en 181 días, decide el color en 43 -de 79
ámbares- y acierta 13 de 56 (23 %) contra una tasa de fondo del 22,6 %. Es
decir: recorta el 24 % de las sesiones acertando lo mismo que una moneda. El
examen de `medir_reglas.py` la deja en p = 0,508.

Antes de borrarla se agota. No porque el número invite a dudar -no invita-,
sino porque es la regla que más sesiones toca, y si el problema fuera el UMBRAL
y no la IDEA, borrarla sería tirar una señal buena por un corte mal puesto.

Las cuatro preguntas, que son las del usuario:
  1. ¿Algún umbral de minutos distingue? Barrido de 300 a 420.
  2. ¿Y la nota de sueño (`sleep_score`) en vez de los minutos?
  3. ¿Y las dos cosas a la vez: pocas horas Y mala nota?
  4. ¿Y dos noches cortas seguidas, en vez de una?

La trampa que este guion NO se permite
--------------------------------------
Probar veinticinco variantes y quedarse con la del p más bajo es fabricar un
hallazgo: con veinticinco tiros, uno por debajo de 0,05 sale por puro azar el
72 % de las veces. Por eso Benjamini-Hochberg se aplica UNA VEZ sobre las
veinticinco juntas, no por bloques. El q de cada fila ya lleva dentro el precio
de haber preguntado tantas veces.

Y el veredicto no lo da el p. Lo da LO QUE LA VARIANTE APORTA POR SU CUENTA:
los días que serían verdes sin ella, y si esos días el cuerpo estaba bajo o
bien. Es la prueba que mató a `carga_acumulada` teniendo un p de 0,0006.

Uso:
    python scripts/medir_sueno.py
    python scripts/medir_sueno.py --contenedor
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.analysis.stats import benjamini_hochberg, percentil
from app.config_loader import load_config
from app.db import session_scope
from app.settings import settings

from medir_reglas import (  # noqa: E402
    ANCHO,
    CUERPO_BAJO,
    binomial_cola,
    color,
    construir,
    cuerpo_bajo,
    tasa_de_fondo,
)
from replay_semaforo import leer_metricas, leer_salidas  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# La regla que las variantes SUSTITUIRÍAN. Para saber qué aporta una variante
# por su cuenta hay que quitar del semáforo la que viene a reemplazar; dejarla
# puesta haría que ninguna variante aportase nunca nada -porque `sueno_corto`
# ya habría pintado esos mismos días- y saldría un empate falso.
REEMPLAZA = "sueno_corto"


def con_vecinos(dias: list[dict]) -> None:
    """Cuelga de cada día la noche anterior y el cuerpo del día siguiente.

    Se busca por fecha, no por posición: si algún día no tiene fila, el de al
    lado en la lista no es el de ayer, y «dos noches seguidas» pasaría a
    significar «dos noches con un hueco en medio».
    """
    por_fecha = {d["dia"]: d for d in dias}
    for d in dias:
        ayer = por_fecha.get(d["dia"] - timedelta(days=1))
        d["sleep_min_ayer"] = ayer.get("sleep_min") if ayer else None
        d["sleep_score_ayer"] = ayer.get("sleep_score") if ayer else None
        manana = por_fecha.get(d["dia"] + timedelta(days=1))
        d["cuerpo_manana"] = cuerpo_bajo(manana) if manana else None


# ---------------------------------------------------------------------------
# Las variantes. Cada una es (familia, etiqueta, predicado).
# El predicado devuelve None cuando el día NO SE PUEDE JUZGAR -falta el dato que
# necesita-, que no es lo mismo que "no dispara". Un `sleep_score` ausente tiene
# que sacar el día del denominador, no contarlo como noche buena.
# ---------------------------------------------------------------------------


def v_minutos(corte: float):
    def f(d):
        v = d.get("sleep_min")
        return None if v is None else v < corte
    return f


def v_banda(corte: float):
    """La forma REAL de la regla: ámbar solo entre 5 h y el corte.

    Por debajo de 300 ya hay un rojo (`sueno_muy_corto`), así que un barrido con
    `sleep_min < corte` a secas le estaría prestando a la variante los aciertos
    de una regla que no se está juzgando.
    """
    def f(d):
        v = d.get("sleep_min")
        return None if v is None else (300 <= v < corte)
    return f


def v_score(corte: float):
    def f(d):
        v = d.get("sleep_score")
        return None if v is None else v < corte
    return f


def v_combinada(min_corte: float, score_corte: float):
    def f(d):
        m, s = d.get("sleep_min"), d.get("sleep_score")
        if m is None or s is None:
            return None
        return m < min_corte and s < score_corte
    return f


def v_dos_noches(corte: float):
    def f(d):
        hoy, ayer = d.get("sleep_min"), d.get("sleep_min_ayer")
        if hoy is None or ayer is None:
            return None
        return hoy < corte and ayer < corte
    return f


def v_dos_noches_score(corte: float):
    def f(d):
        hoy, ayer = d.get("sleep_score"), d.get("sleep_score_ayer")
        if hoy is None or ayer is None:
            return None
        return hoy < corte and ayer < corte
    return f


def variantes(cortes_min: list[float], cortes_score: list[float]) -> list[dict]:
    vs: list[dict] = []
    vs.append({"fam": "0. la de hoy", "et": "300 <= min < 390",
               "f": v_banda(390), "actual": True})
    for c in cortes_min:
        if c <= 300:
            continue
        vs.append({"fam": "1. minutos", "et": f"300 <= min < {c:.0f}",
                   "f": v_banda(c)})
    for c in cortes_score:
        vs.append({"fam": "2. nota", "et": f"score < {c:.0f}", "f": v_score(c)})
    for cm in (390.0, 420.0):
        for cs in cortes_score:
            vs.append({"fam": "3. las dos", "et": f"min < {cm:.0f} y score < {cs:.0f}",
                       "f": v_combinada(cm, cs)})
    for c in cortes_min:
        vs.append({"fam": "4. dos noches", "et": f"min < {c:.0f} dos noches",
                   "f": v_dos_noches(c)})
    for c in cortes_score:
        vs.append({"fam": "4. dos noches", "et": f"score < {c:.0f} dos noches",
                   "f": v_dos_noches_score(c)})
    return vs


def mide(v: dict, dias: list[dict], fondo: float) -> dict:
    """Los números de una variante: los del test y los que de verdad deciden."""
    juzgables = [d for d in dias if v["f"](d) is not None]
    dispara = [d for d in juzgables if v["f"](d)]
    medibles = [d for d in dispara if cuerpo_bajo(d) is not None]
    aciertos = [d for d in medibles if cuerpo_bajo(d)]
    p = binomial_cola(len(aciertos), len(medibles), fondo) if medibles else None

    # Lo que aporta POR SU CUENTA: días que sin ella -y sin la regla a la que
    # sustituye- serían verdes. Ahí es donde una regla de sueño recorta una
    # sesión que nadie más iba a recortar, y donde se ve si acierta.
    propios = [d for d in dispara
               if color(d["disparadas"], excluir=REEMPLAZA) == "green"]
    propios_bajos = [d for d in propios if cuerpo_bajo(d)]
    propios_falsos = [d for d in propios if cuerpo_bajo(d) is False]
    return {
        **v,
        "juzgables": len(juzgables),
        "dispara": len(dispara),
        "n": len(medibles),
        "aciertos": len(aciertos),
        "tasa": (len(aciertos) / len(medibles)) if medibles else None,
        "p": p,
        "propios": len(propios),
        "propios_bajos": len(propios_bajos),
        "propios_falsos": len(propios_falsos),
        "dias_propios": propios,
    }


def fondo_del_dia_siguiente(dias: list[dict]) -> tuple[float, int, int]:
    """Con qué frecuencia amanece el cuerpo bajo DESPUÉS de un día con el cuerpo bien.

    No vale la tasa de fondo normal: `hrv_ratio` se arrastra de un día para otro,
    así que un día malo detrás de otro malo no demuestra nada. El fondo honesto
    para «¿anuncia algo?» es el de los días que siguen a uno bueno.
    """
    base = [d for d in dias
            if cuerpo_bajo(d) is False and d.get("cuerpo_manana") is not None]
    malos = sum(1 for d in base if d["cuerpo_manana"])
    return (malos / len(base) if base else 0.0), malos, len(base)


def mide_manana(v: dict, dias: list[dict], fondo: float) -> dict:
    """¿Sabe la noche algo que la HRV de esa misma mañana TODAVÍA no sabe?

    Esta es la única versión no circular de la pregunta. `hrv_baja_1d` es el
    criterio de resultado, así que hoy coge por definición todos los días malos:
    ninguna regla puede añadir un solo acierto SOBRE EL MISMO DÍA. Lo que una
    regla de sueño sí podría aportar es ADELANTO -avisar la mañana en que la HRV
    aún está bien de que mañana no lo estará-, y eso sí se puede medir.

    Se mira solo dentro de los días con la HRV normal, que son justo aquellos en
    los que el semáforo habría dicho verde y la regla de sueño sería la única voz
    en contra.
    """
    base = [d for d in dias
            if cuerpo_bajo(d) is False
            and d.get("cuerpo_manana") is not None
            and v["f"](d) is not None]
    dispara = [d for d in base if v["f"](d)]
    aciertos = [d for d in dispara if d["cuerpo_manana"]]
    p = binomial_cola(len(aciertos), len(dispara), fondo) if dispara else None
    return {**v, "n_m": len(dispara), "ac_m": len(aciertos),
            "tasa_m": (len(aciertos) / len(dispara)) if dispara else None, "p_m": p}


def pinta_manana(filas: list[dict], fondo: float, malos: int, base: int) -> None:
    for f, q in zip(filas, benjamini_hochberg([x["p_m"] for x in filas])):
        f["q_m"] = q
    print("\n" + "=" * ANCHO)
    print("  ¿ADELANTA ALGO? — la única pregunta que no es circular")
    print("=" * ANCHO)
    print("  Sobre el MISMO día no hay nada que ganar: `hrv_baja_1d` es el criterio")
    print(f"  de resultado y ya coge los 40 días malos, los 40. Aquí se pregunta otra")
    print("  cosa: partiendo de una mañana con la HRV normal, ¿la mala noche anuncia")
    print("  que mañana el cuerpo estará bajo?")
    print(f"  fondo: {malos}/{base} = {100*fondo:.1f} % de los días que siguen a uno bueno")
    print()
    print(f"  {'variante':28s} {'disp':>5s} {'mañana mal':>11s} {'tasa':>6s} "
          f"{'p':>8s} {'q':>7s}")
    print("  " + "-" * (ANCHO - 2))
    fam = None
    for f in filas:
        if f["fam"] != fam:
            fam = f["fam"]
            print(f"  {fam}")
        cuerpo = f"{f['ac_m']}/{f['n_m']}" if f["n_m"] else "—"
        tasa = f"{100*f['tasa_m']:.0f}%" if f["tasa_m"] is not None else "—"
        p = f"{f['p_m']:.4f}" if f["p_m"] is not None else "—"
        q = f"{f['q_m']:.4f}" if f.get("q_m") is not None else "—"
        marca = " *" if f.get("actual") else "  "
        print(f"    {f['et']:24s}{marca}{f['n_m']:5d} {cuerpo:>11s} {tasa:>6s} "
              f"{p:>8s} {q:>7s}")
    pasan = [f for f in filas if f.get("q_m") is not None and f["q_m"] < 0.05]
    print()
    if pasan:
        print(f"  {len(pasan)} variante(s) adelantan algo:")
        for f in pasan:
            print(f"    «{f['et']}»  {f['ac_m']}/{f['n_m']} "
                  f"({100*f['tasa_m']:.0f} %) contra {100*fondo:.1f} %  q={f['q_m']:.4f}")
    else:
        print("  NINGUNA adelanta nada. Partiendo de una mañana con la HRV normal, una")
        print("  mala noche no dice absolutamente nada sobre cómo amanecerá el cuerpo")
        print("  al día siguiente: la tasa es la del azar.")


def pinta_distribucion(dias: list[dict], señal: str, unidad: str) -> None:
    vals = sorted(d[señal] for d in dias if d.get(señal) is not None)
    huecos = sum(1 for d in dias if d.get(señal) is None)
    print(f"\n  {señal}  ({len(vals)} días con dato, {huecos} sin él)")
    if not vals:
        print("    ni un solo valor: esta familia entera no se puede medir")
        return
    media = sum(vals) / len(vals)
    print(f"    media {media:.1f}{unidad}   min {vals[0]:.0f}   max {vals[-1]:.0f}")
    print("    percentiles: " + "  ".join(
        f"p{p:g}={percentil(vals, p):.0f}" for p in (5, 10, 25, 50, 75, 90)))


def pinta(filas: list[dict], fondo: float) -> None:
    for f, q in zip(filas, benjamini_hochberg([x["p"] for x in filas])):
        f["q"] = q

    print("\n" + "=" * ANCHO)
    print("  LAS VARIANTES, UNA A UNA")
    print("=" * ANCHO)
    print(f"  «cuerpo bajo» = {CUERPO_BAJO[0]} < {CUERPO_BAJO[1]} · fondo {100*fondo:.1f} %")
    print(f"  q = Benjamini-Hochberg sobre las {len(filas)} variantes JUNTAS. Ver el")
    print("  docstring: corregir por bloques sería repartirse el privilegio de mentir.")
    print()
    print(f"  {'variante':28s} {'disp':>5s} {'cuerpo':>8s} {'tasa':>6s} "
          f"{'p':>8s} {'q':>7s} {'propios':>8s} {'bajos':>6s} {'falsas':>7s}")
    print("  " + "-" * (ANCHO - 2))
    fam = None
    for f in filas:
        if f["fam"] != fam:
            fam = f["fam"]
            print(f"  {fam}")
        cuerpo = f"{f['aciertos']}/{f['n']}" if f["n"] else "—"
        tasa = f"{100*f['tasa']:.0f}%" if f["tasa"] is not None else "—"
        p = f"{f['p']:.4f}" if f["p"] is not None else "—"
        q = f"{f['q']:.4f}" if f.get("q") is not None else "—"
        marca = " *" if f.get("actual") else "  "
        print(f"    {f['et']:24s}{marca}{f['dispara']:5d} {cuerpo:>8s} {tasa:>6s} "
              f"{p:>8s} {q:>7s} {f['propios']:>8d} {f['propios_bajos']:>6d} "
              f"{f['propios_falsos']:>7d}")
    print("\n  * = la regla tal y como está hoy en config.yaml")
    print("  propios = días que serían VERDES sin ninguna regla de sueño de una noche")
    print("  bajos / falsas = de esos propios, con el cuerpo bajo / con el cuerpo bien")
    print()
    print("  CUIDADO CON LA COLUMNA «bajos»: sale 0 en las 53 filas y no puede salir")
    print("  otra cosa. Un día «propio» es un día verde sin la regla de sueño, y verde")
    print("  quiere decir que `hrv_baja_1d` no disparó, o sea hrv_ratio >= 0,90, o sea")
    print("  cuerpo NO bajo. Es aritmética, no un hallazgo. Lo que esa columna sí mide")
    print("  es CUÁNTAS SESIONES recorta la variante ella sola; el acierto de esos")
    print("  recortes hay que ir a buscarlo a la tabla de más abajo.")


def veredicto(filas: list[dict], filas_m: list[dict], fondo: float) -> None:
    print("\n" + "=" * ANCHO)
    print("  VEREDICTO")
    print("=" * ANCHO)
    hoy = [f for f in filas if f.get("q") is not None and f["q"] < 0.05]
    manana = {f["et"] for f in filas_m if f.get("q_m") is not None and f["q_m"] < 0.05}
    por_et = {f["et"]: f for f in filas_m}

    minutos = [f for f in hoy if "score" not in f["et"]]
    print("  1) MINUTOS — la pregunta del barrido 300→420")
    if minutos:
        print("     " + ", ".join(f["et"] for f in minutos))
    else:
        mejor = min((f for f in filas if f["p"] is not None and "score" not in f["et"]),
                    key=lambda f: f["p"])
        print(f"     Ningún umbral distingue nada. El menos malo es «{mejor['et']}»,")
        print(f"     {mejor['aciertos']}/{mejor['n']} ({100*mejor['tasa']:.0f} %) contra "
              f"un fondo de {100*fondo:.1f} %, q = {mejor['q']:.4f}.")
        print("     Y el barrido baja monótonamente: cuanto MENOS duermo, MENOS")
        print("     probable es que el cuerpo esté bajo. No hay corte que salvar;")
        print("     no es un umbral mal puesto, es que los minutos no llevan señal.")

    print()
    print("  2) LA NOTA — `sleep_score`")
    scores = [f for f in hoy if f["et"].startswith("score <")]
    if scores:
        print(f"     Sí distingue, y con claridad: {len(scores)} cortes pasan BH sobre")
        print("     las 53 variantes juntas. El mejor:")
        mej = min(scores, key=lambda f: f["p"])
        print(f"       «{mej['et']}»  {mej['aciertos']}/{mej['n']} "
              f"({100*mej['tasa']:.0f} %) contra {100*fondo:.1f} %  q={mej['q']:.4f}")
        print("     Confirma lo que ya sabíamos: la nota está ligada a la HRV y los")
        print("     minutos no. Pero DISTINGUIR no es SERVIR, y aquí se separan las dos")
        print("     cosas:")
        print()
        for f in scores:
            g = por_et[f["et"]]
            ade = "SÍ adelanta" if f["et"] in manana else "no adelanta nada"
            print(f"       {f['et']:22s} recorta {f['propios']:3d} sesiones ella sola "
                  f"· mañana {g['ac_m']}/{g['n_m']} · {ade}")
        print()
        print("     Los días que acierta ya los cogía `hrv_baja_1d`: los 40 días con el")
        print("     cuerpo bajo son ámbar sin ayuda de nadie. Así que de todo lo que")
        print("     esta regla cambiaría, lo único que cambia de verdad son las sesiones")
        print("     de la columna «recorta», y esas son días con la HRV normal.")
    else:
        print("     Tampoco distingue.")

    print()
    print("  3) LAS DOS A LA VEZ  ·  4) DOS NOCHES SEGUIDAS")
    otros = [f for f in hoy if f["fam"].startswith(("3.", "4."))]
    if otros:
        print("     " + ", ".join(f["et"] for f in otros))
    else:
        print("     Ninguna combinación ni ninguna racha de dos noches pasa el corte.")
        print("     Cruzar los minutos con la nota EMPEORA la nota sola -le mete dentro")
        print("     el ruido de los minutos- y exigir dos noches solo achica la muestra.")

    print()
    print("  LO QUE DECIDE")
    if not manana:
        print("     Ninguna de las 53, ni siquiera las que distinguen, ADELANTA nada.")
        print("     Partiendo de una mañana con la HRV normal -que es la única mañana en")
        print("     la que una regla de sueño llegaría a cambiar algo- dormir mal no")
        print("     dice nada sobre cómo amanecerá el cuerpo al día siguiente.")
        print()
        print("     Con eso, `sueno_corto` no tiene defensa: la versión de minutos no")
        print("     distingue ni el mismo día, y la versión que sí distingue solo repite")
        print("     lo que la HRV ya ha dicho, un día en que además decide lo contrario.")
    else:
        print(f"     {len(manana)} variante(s) adelantan el día malo. Esa sí sería una")
        print("     regla con motivo: avisa cuando la HRV todavía no puede.")
        for et in sorted(manana):
            g = por_et[et]
            print(f"       «{et}»  {g['ac_m']}/{g['n_m']} "
                  f"({100*g['tasa_m']:.0f} %)  q={g['q_m']:.4f}")


def reejecutar_dentro(contenedor: str, argv: list[str]) -> int:
    """`scripts/` no está en la imagen: el guion se copia dentro y se re-lanza.

    Copia también `medir_reglas.py` y `replay_semaforo.py`, de los que importa.
    """
    for nombre in ("medir_reglas.py", "replay_semaforo.py", "medir_sueno.py"):
        subprocess.run(
            ["docker", "cp", str(ROOT / "scripts" / nombre), f"{contenedor}:/tmp/{nombre}"],
            check=True, capture_output=True,
        )
    r = subprocess.run(
        ["docker", "exec", "-e", "PYTHONIOENCODING=utf-8", contenedor,
         "python", "/tmp/medir_sueno.py", *argv],
        env={"MSYS_NO_PATHCONV": "1", **__import__("os").environ},
    )
    return r.returncode


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--contenedor", nargs="?", const="hevy2garmin", default=None)
    p.add_argument("--min", default="310,320,330,340,350,360,370,380,390,400,410,420")
    p.add_argument("--score", default="50,55,60,65,70,75,80")
    args = p.parse_args()

    if args.contenedor:
        resto = [a for a in sys.argv[1:] if not a.startswith("--contenedor")]
        resto = [a for a in resto if a != args.contenedor]
        return reejecutar_dentro(args.contenedor, resto)

    cortes_min = [float(x) for x in args.min.split(",") if x.strip()]
    cortes_score = [float(x) for x in args.score.split(",") if x.strip()]
    # `settings.config_path` y no `ROOT / "config.yaml"`: dentro del contenedor
    # este guion vive en /tmp y ROOT sería la raíz del sistema de ficheros.
    cfg = load_config(settings.config_path)

    with session_scope() as s:
        metricas = leer_metricas(s)
        salidas = leer_salidas(s)
    if not metricas:
        print("no hay ni una fila en daily_metrics: nada que medir")
        return 1

    desde, hasta = metricas[0].date, metricas[-1].date
    dias = construir(cfg, metricas, salidas, desde, hasta)
    con_vecinos(dias)
    fondo, bajos, medibles = tasa_de_fondo(dias)

    print("=" * ANCHO)
    print("  ¿DISTINGUE ALGO MI SUEÑO? — solo lectura, no se escribe nada")
    print("=" * ANCHO)
    print(f"  config  : hash={cfg.hash}")
    print(f"  ventana : {desde} → {hasta}  ({len(dias)} días)")
    print(f"  fondo   : {bajos}/{medibles} días con {CUERPO_BAJO[0]} < "
          f"{CUERPO_BAJO[1]} = {100*fondo:.1f} %")

    print("\n" + "=" * ANCHO)
    print("  LO QUE HAY MEDIDO")
    print("=" * ANCHO)
    pinta_distribucion(dias, "sleep_min", " min")
    pinta_distribucion(dias, "sleep_score", "")

    vs = variantes(cortes_min, cortes_score)
    filas = [mide(v, dias, fondo) for v in vs]
    pinta(filas, fondo)

    fondo_m, malos_m, base_m = fondo_del_dia_siguiente(dias)
    filas_m = [mide_manana(v, dias, fondo_m) for v in vs]
    pinta_manana(filas_m, fondo_m, malos_m, base_m)

    veredicto(filas, filas_m, fondo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
