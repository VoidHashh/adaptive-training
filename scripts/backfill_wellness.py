"""Reconstruir el histórico de bienestar que nunca se llegó a guardar.

`daily_metrics` solo se escribe cuando el sistema corre. Antes de que existiera,
y cada día que el PC estuvo apagado, no hay fila: y en la base de datos un día
ausente y un día sin reloj son la misma celda vacía. Las vistas 1 y 2 emparejan
el formulario de cada mañana con el Garmin de esa mañana, así que cada día sin
fila es una pareja menos, y no una elegida al azar.

El sondeo de `scripts/sondeo_wellness.py` midió hasta dónde sirve Garmin hacia
atrás (2026-09-11):

    HRV, FC en reposo, minutos de sueño, nota de sueño  -> hasta -175 días
    body battery                                        -> hasta -120 días
    training readiness                                  -> nunca, ni ayer

Lo que ya no sirve queda como hueco explícito en la fila, nunca como cero.

ESTO TARDA, Y TARDA A PROPÓSITO
-------------------------------
Cuatro peticiones por día. Ciento ochenta días son setecientas veinte, y Garmin
corta por IP durante minutos cuando se le pide demasiado seguido -devolvió 429
durante el sondeo-. A dos segundos por día son unos veinticinco minutos. Es el
precio de que termine.

Es reanudable sin llevar la cuenta en ninguna parte: escribe día a día, y un
corte a mitad deja guardado todo lo anterior. Volver a lanzarlo sigue por donde
se quedó, porque lo pendiente se deduce de la propia base -sin fila, o con fila
marcada `error`-.

    python scripts/backfill_wellness.py --simular
    python scripts/backfill_wellness.py
    python scripts/backfill_wellness.py --dias 60 --pausa 3
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from app.backfill import METRICAS, PETICIONES_POR_DIA, dias_pendientes, rellenar
from app.config_loader import load_config
from app.db import init_db, session_scope
from app.settings import settings

ETIQUETAS = {
    "hrv": "HRV",
    "rhr": "FC reposo",
    "sleep_min": "sueño (min)",
    "sleep_score": "sueño (nota)",
    "body_battery": "body battery",
}


def main() -> int:
    cfg = load_config(settings.config_path)
    bf = (cfg.wellness.get("backfill") or {})

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dias", type=int, default=int(bf.get("history_days") or 180),
        help="cuántos días hacia atrás (por defecto, wellness.backfill.history_days)",
    )
    p.add_argument(
        "--pausa", type=float, default=float(bf.get("pause_seconds") or 2.0),
        help="segundos entre días (por defecto, wellness.backfill.pause_seconds)",
    )
    p.add_argument(
        "--simular", action="store_true",
        help="dice qué días faltan y cuánto costaría, sin tocar Garmin",
    )
    args = p.parse_args()

    init_db()

    hoy = date.today()
    # Acaba anteayer por lo mismo que la recuperación de arranque: los datos de
    # anoche llegan cuando el reloj sincroniza, y una fila `partial` no se
    # reintenta nunca. De los dos últimos días se encarga la ventana diaria.
    hasta = hoy - timedelta(days=2)
    desde = hoy - timedelta(days=args.dias)

    with session_scope() as s:
        pendientes = dias_pendientes(s, desde, hasta)

    total = (hasta - desde).days + 1
    print(f"ventana   {desde} .. {hasta}  ({total} días)")
    print(f"ya están  {total - len(pendientes)}")
    print(f"faltan    {len(pendientes)}")
    if not pendientes:
        print("\nNo hay nada que recuperar.")
        return 0

    peticiones = len(pendientes) * PETICIONES_POR_DIA
    minutos = (len(pendientes) - 1) * args.pausa / 60
    print(f"coste     {peticiones} peticiones, ~{minutos:.0f} min de pausas")

    if args.simular:
        print("\n(simulación: no se ha pedido nada)")
        print("primeros:", ", ".join(str(d) for d in pendientes[:5]))
        print("últimos: ", ", ".join(str(d) for d in pendientes[-5:]))
        return 0

    from app.integrations.garmin import build_client

    cliente = build_client(settings, cfg)
    cliente.connect()
    print("\nconectado. empieza el relleno; se puede cortar y reanudar.\n")

    hecho = [0]

    def progreso(dia: date, m, errores: list[str]) -> None:
        hecho[0] += 1
        trozos = []
        for nombre in METRICAS:
            v = getattr(m, nombre, None)
            trozos.append(f"{ETIQUETAS[nombre]}={'—' if v is None else v}")
        print(
            f"  [{hecho[0]:>3}/{len(pendientes)}] {dia}  " + "  ".join(trozos)
        )
        for e in errores:
            print(f"        ! {e}")

    with session_scope() as s:
        res = rellenar(s, cliente, pendientes, pausa=args.pausa, progreso=progreso)

    print("\n" + "=" * 74)
    print("RESULTADO")
    print("=" * 74)
    print(" ", res.resumen())

    if res.huecos:
        print("\nHUECOS POR MÉTRICA (Garmin contestó, pero no había dato)")
        for nombre in METRICAS:
            dias = res.huecos.get(nombre) or []
            if not dias:
                print(f"  {ETIQUETAS[nombre]:16s} ninguno")
                continue
            # El día MÁS RECIENTE con hueco es el dato útil: marca dónde deja
            # de servirse esa métrica. Los de más atrás son consecuencia.
            print(
                f"  {ETIQUETAS[nombre]:16s} {len(dias)} día(s), "
                f"el más reciente {max(dias)}"
            )

    if res.errores:
        print(f"\nFALLOS ({len(res.errores)}), se reintentan solos al repetir:")
        for e in res.errores[:20]:
            print(f"  ! {e}")
        if len(res.errores) > 20:
            print(f"  ... y {len(res.errores) - 20} más")

    if res.interrumpido:
        print(f"\n{res.interrumpido}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
