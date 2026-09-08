"""Auditoría de la sección `safety`: qué bloquea, dónde y a quién.

Existe para responder a una pregunta concreta: ¿algún patrón de nombre está
capturando un ejercicio que en este caso es válido? El patrón /peso muerto/
existe y el peso muerto en máquina guiada del Día 2 SÍ coincide con él a nivel
de texto, así que la única defensa es el ÁMBITO: la restricción se evalúa solo
sobre las rutinas HIIT. Este script no confía en esa afirmación, la mide: aplica
el matcher a TODAS las rutinas y marca cuáles caerían si el ámbito fuese global,
para que se vea la diferencia entre "no coincide" y "coincide pero no se mira".
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

SAFETY = RAW["safety"]["forbidden_in_hiit"]
IDS = SAFETY.get("template_ids", [])
PATTERNS = SAFETY.get("name_patterns", [])
EXCEPTIONS = SAFETY.get("allow_exceptions", [])
ROUTINES = RAW["routines"]
HIIT_ROUTINES = set((RAW.get("hiit", {}).get("blocks") or {}).values())


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def match_patterns(name: str) -> list[str]:
    norm = strip_accents(name)
    return [p for p in PATTERNS if re.search(p, norm, re.IGNORECASE)]


# --- 1. el listado -----------------------------------------------------------
print("=" * 78)
print(f"BLOQUEADOS POR template_id: {len(IDS)}")
print("=" * 78)

# Los comentarios de grupo del YAML se pierden al parsear, así que los
# reconstruyo por posición leyendo el fichero en crudo.
lines = (ROOT / "config.yaml").read_text(encoding="utf-8").splitlines()
group = "(sin grupo)"
groups: dict[str, list[dict]] = {}
in_ids = False
for ln in lines:
    st = ln.strip()
    if st.startswith("template_ids:"):
        in_ids = True
        continue
    if in_ids and st.startswith("name_patterns:"):
        break
    if not in_ids:
        continue
    if st.startswith("# ---"):
        group = st.strip("# -").strip()
        continue
    m = re.search(r'id:\s*"?([0-9A-Fa-f]+)"?', st)
    if m:
        nm = re.search(r'name:\s*"?([^"}]+)"?', st)
        groups.setdefault(group, []).append(
            {"id": m.group(1).upper(), "name": (nm.group(1) if nm else "?").strip(" }")}
        )

total = 0
for gname, items in groups.items():
    print(f"\n  [{gname}]  ({len(items)})")
    for it in items:
        total += 1
        print(f"    {total:>2}. {it['id']}  {it['name']}")
print(f"\n  total listado = {total} / declarados = {len(IDS)}")

print()
print("=" * 78)
print(f"PATRONES DE NOMBRE: {len(PATTERNS)}")
print("=" * 78)
for i, p in enumerate(PATTERNS, 1):
    print(f"  {i:>2}. /{p}/")

print()
print("=" * 78)
print(f"EXCEPCIONES EXPLÍCITAS: {len(EXCEPTIONS)}")
print("=" * 78)
for e in EXCEPTIONS:
    print(f"  {e['id']}  {e.get('name', '?')}")
    print(f"      motivo: {e.get('why', '—')}")

# --- 2. la pregunta: ¿qué capturarían los patrones si el ámbito fuese global? -
print()
print("=" * 78)
print("SIMULACRO: patrones aplicados a TODAS las rutinas (ámbito hipotético)")
print("=" * 78)
print(f"  rutinas HIIT reales (única zona donde se evalúa): {sorted(HIIT_ROUTINES) or '—'}")
print()

falsos_positivos = 0
for rkey in sorted(ROUTINES):
    hits = []
    for ex in ROUTINES[rkey].get("exercises", []):
        name = ex.get("name", "")
        tid = str(ex.get("template_id") or "").upper()
        by_id = next((e for e in IDS if str(e["id"]).upper() == tid), None)
        by_pat = match_patterns(name)
        if by_id or by_pat:
            hits.append((ex.get("key"), name, tid, by_id, by_pat))
    if not hits:
        continue
    scope = "HIIT — SE EVALÚA" if rkey in HIIT_ROUTINES else "fuerza — NO se evalúa"
    print(f"  {rkey}  [{scope}]")
    for key, name, tid, by_id, by_pat in hits:
        print(f"    · {key}  \"{name}\"  (template {tid})")
        if by_id:
            print(f"        id en lista negra: {by_id['name']}")
        for p in by_pat:
            print(f"        coincide patrón:   /{p}/")
        if rkey not in HIIT_ROUTINES:
            falsos_positivos += 1
            print("        -> INOFENSIVO aquí: el ámbito es solo HIIT")
    print()

# --- 3. veredicto ------------------------------------------------------------
print("=" * 78)
print("VEREDICTO")
print("=" * 78)

target = None
for ex in ROUTINES.get("dia_2", {}).get("exercises", []):
    if ex.get("key") == "peso_muerto_smith":
        target = ex
        break

assert target is not None, "peso_muerto_smith no está en dia_2"
name = target["name"]
tid = str(target["template_id"]).upper()
pats = match_patterns(name)
by_id = next((e for e in IDS if str(e["id"]).upper() == tid), None)

print(f"  dia_2.peso_muerto_smith")
print(f"    name en YAML : \"{name}\"")
print(f"    template_id  : {tid}")
print(f"    ¿id en lista negra? {'SÍ -> ' + by_id['name'] if by_id else 'no'}")
print(f"    ¿coincide patrón?   {'SÍ -> ' + ', '.join('/' + p + '/' for p in pats) if pats else 'no'}")
print(f"    ¿dia_2 es rutina HIIT? {'sí' if 'dia_2' in HIIT_ROUTINES else 'NO'}")
print()
if (by_id or pats) and "dia_2" not in HIIT_ROUTINES:
    print("  => COINCIDE, PERO NO SE BLOQUEA. La restricción solo recorre las")
    print("     rutinas de hiit.blocks. dia_2 es fuerza, así que el ejercicio")
    print("     entra intacto. Lo que lo vigila aquí es `retirada_peso_muerto`.")
elif not (by_id or pats):
    print("  => No coincide con nada.")
else:
    print("  => BLOQUEADO. Revisar.")

print()
print(f"  ejercicios de fuerza que coinciden pero no se evalúan: {falsos_positivos}")
print("  (si algún día se convierte una de esas rutinas en HIIT, saltarán)")
