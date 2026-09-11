"""¿Qué wellness sirve Garmin hacia atrás, y hasta dónde?

El código lleva meses afirmando en un comentario que "Garmin no sirve histórico
antiguo de sueño ni de body battery". Puede que sea cierto y puede que no; nadie
lo ha comprobado nunca, y de esa frase depende si las vistas 1 y 2 arrancan con
seis meses de datos o con cero.

Esto lo mide en vez de suponerlo. Se piden DÍAS SUELTOS de antigüedad creciente
-3, 7, 14, 30, 60, 90, 120, 150, 175 días- en vez de diez días seguidos: la
pregunta no es "¿hay datos de la semana pasada?" sino "¿dónde se acaban?", y
diez días consecutivos solo contestan la primera.

Cuesta cinco peticiones por día sondeado. Con la pausa por defecto son unos
cuarenta segundos y no despeina el cupo; el backfill entero de 180 días son
novecientas, y por eso se sondea antes.

    python scripts/sondeo_wellness.py
    python scripts/sondeo_wellness.py --pausa 4 --edades 3,30,180
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EDADES = (3, 7, 14, 30, 60, 90, 120, 150, 175)

# (atributo de DayMetrics, cómo se llama en el informe)
METRICAS = (
    ("hrv", "HRV"),
    ("rhr", "FC reposo"),
    ("sleep_min", "sueño (min)"),
    ("sleep_score", "sueño (nota)"),
    ("body_battery", "body battery"),
    ("readiness", "readiness"),
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pausa", type=float, default=3.0, help="segundos entre días")
    p.add_argument("--edades", default=",".join(str(e) for e in EDADES))
    args = p.parse_args()

    edades = [int(x) for x in args.edades.split(",") if x.strip()]

    from app.config_loader import load_config
    from app.integrations.garmin import build_client
    from app.settings import settings

    cfg = load_config(settings.config_path)
    cliente = build_client(settings, cfg)
    cliente.connect()
    print(f"conectado. sondeando {len(edades)} días ({len(edades) * 5} peticiones)\n")

    hoy = date.today()
    filas = []
    for i, edad in enumerate(edades):
        dia = hoy - timedelta(days=edad)
        antes = len(cliente.fetch_errors)
        try:
            m = cliente.day_metrics(dia)
        except Exception as exc:  # noqa: BLE001
            print(f"  {dia} (-{edad:>3}d)  LA PETICIÓN FALLÓ: {exc}")
            filas.append((edad, dia, None, []))
            continue
        nuevos = cliente.fetch_errors[antes:]
        filas.append((edad, dia, m, nuevos))

        trozos = []
        for attr, etiqueta in METRICAS:
            v = getattr(m, attr, None)
            trozos.append(f"{etiqueta}={'—' if v is None else v}")
        print(f"  {dia} (-{edad:>3}d)  " + "  ".join(trozos))
        for n in nuevos:
            print(f"      ! {n}")

        if i < len(edades) - 1:
            time.sleep(args.pausa)

    print("\n" + "=" * 70)
    print("QUÉ SE RECUPERA, POR MÉTRICA")
    print("=" * 70)
    for attr, etiqueta in METRICAS:
        vivos = [e for e, _d, m, _n in filas if m is not None and getattr(m, attr) is not None]
        if not vivos:
            print(f"  {etiqueta:16s} NADA en ninguna de las edades sondeadas")
            continue
        print(
            f"  {etiqueta:16s} {len(vivos)}/{len(filas)} días, "
            f"el más antiguo a -{max(vivos)}d"
        )

    # Lo que de verdad decide si merece la pena el backfill largo: que HRV y FC
    # en reposo vuelvan de lejos. Son las dos que alimentan las parejas de la
    # vista 1 y las líneas base del motor.
    print()
    for attr, etiqueta in (("hrv", "HRV"), ("rhr", "FC reposo")):
        vivos = [e for e, _d, m, _n in filas if m is not None and getattr(m, attr) is not None]
        if vivos and max(vivos) >= 90:
            print(f"  -> {etiqueta}: SÍ vuelve de lejos (hasta -{max(vivos)}d). Backfill largo justificado.")
        elif vivos:
            print(f"  -> {etiqueta}: solo vuelve hasta -{max(vivos)}d. El backfill se queda en esa ventana.")
        else:
            print(f"  -> {etiqueta}: NO vuelve. Solo se puede acumular hacia delante.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
