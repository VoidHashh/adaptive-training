"""Comprueba que el patrón acotado de v-up captura lo que debe y nada más."""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import yaml

ROOT = Path(__file__).resolve().parents[1]
raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
pats = raw["safety"]["forbidden_in_hiit"]["name_patterns"]
new = next(p for p in pats if "ups?" in p)
old = "v.?up"

print(f"patrones totales: {len(pats)}")
print(f"antiguo: /{old}/")
print(f"nuevo:   /{new}/\n")

DEBE_CAPTURAR = ["V Up", "V-Up", "vup", "V Ups", "v-ups", "V UP (Weighted)"]
NO_DEBE = [
    "Toes to Bar", "Elevacion de piernas", "Curl femoral de pie",
    "Press militar", "Serve up", "Warm up set", "Level up", "Move up drill",
    "Reverse up", "Bulgarian split squat", "Hip thrust", "Pullover",
    # Estos dos son los que de verdad distinguen los dos patrones: el antiguo
    # no exigía frontera de palabra antes de la `v`, así que cualquier palabra
    # acabada en "v" pegada a "up" entraba.
    "Rev up bike drill", "Revup", "Sled shove up ramp",
]


def norm(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


fallos = 0
print("  debe capturar:")
for c in DEBE_CAPTURAR:
    hit = bool(re.search(new, norm(c), re.IGNORECASE))
    ok = "ok " if hit else "FALLO"
    fallos += 0 if hit else 1
    print(f"    [{ok}] {c}")

print("\n  NO debe capturar:")
for c in NO_DEBE:
    hit = bool(re.search(new, norm(c), re.IGNORECASE))
    was = bool(re.search(old, norm(c), re.IGNORECASE))
    ok = "FALLO" if hit else "ok "
    fallos += 1 if hit else 0
    nota = "   (el antiguo SÍ lo capturaba)" if was and not hit else ""
    print(f"    [{ok}] {c}{nota}")

print()
print("TODO OK" if fallos == 0 else f"{fallos} FALLOS")
sys.exit(1 if fallos else 0)
