"""Los ajustes de entorno, y sobre todo los que no se aplican.

`Settings` estaba con `extra="ignore"`, que suena inofensivo y no lo es. Un
`DRY_RUM=true` en el `.env` se leía, se descartaba y el arranque continuaba con
`dry_run=False`. El usuario había pedido expresamente que no se escribiera en
Hevy ni se enviara Telegram, el sistema entendió que sí, y nadie dijo nada.

`forbid` tapa eso, pero solo donde hay fichero `.env`. Dentro de Docker no lo
hay -está en `.dockerignore` a propósito- y los valores llegan como variables
de entorno, donde pydantic solo mira las que ya conoce. O sea que el agujero
seguía abierto justo en producción. Eso lo tapa `erratas_de_entorno`.

El otro asunto de este fichero es que `.env.example` diga la verdad: era la
única documentación de qué ajustes existen, le faltaban cuatro, y en el único
que explicaba recomendaba lo contrario del valor que traía.
"""

from __future__ import annotations

import pytest

from app.settings import REPO_ROOT, Settings, erratas_de_entorno

EJEMPLO = REPO_ROOT / ".env.example"

CAMPOS = set(Settings.model_fields)


# ---------------------------------------------------------------------------
# Un ajuste mal escrito
# ---------------------------------------------------------------------------


def test_una_errata_en_el_env_no_se_ignora(tmp_path, monkeypatch):
    """El caso original, con fichero: la CLI y el desarrollo local."""
    env = tmp_path / ".env"
    env.write_text("GARMIN_EMAIL=a@b.c\nDRY_RUM=true\n", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        Settings(_env_file=env)
    assert "DRY_RUM" in str(exc.value) or "dry_rum" in str(exc.value).lower()


def test_un_env_correcto_sigue_cargando(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GARMIN_EMAIL=a@b.c\nDRY_RUN=true\n", encoding="utf-8")
    s = Settings(_env_file=env)
    assert s.garmin_email == "a@b.c"
    assert s.dry_run is True


# ---------------------------------------------------------------------------
# Lo mismo sin fichero: el caso Docker
# ---------------------------------------------------------------------------


def test_las_variables_de_otro_no_impiden_arrancar():
    """Lo que trae la imagen base y el `TZ` de compose no son asunto nuestro.

    Si esto fallara, la corrección sería peor que el fallo: un contenedor que
    se niega a arrancar por una variable ajena es un sistema que no decide
    ningún día, y eso se nota más tarde que un `DRY_RUN` mal escrito.
    """
    ajenas = {
        "PATH": "/usr/bin", "HOME": "/home/app", "HOSTNAME": "abc123",
        "TZ": "Europe/Madrid", "LANG": "C.UTF-8", "GPG_KEY": "ABC",
        "PYTHON_VERSION": "3.11.9", "PYTHONUNBUFFERED": "1",
        "VIRTUAL_ENV": "/opt/venv", "PWD": "/app", "TERM": "xterm",
        "PYTHON_SHA256": "deadbeef", "DEBIAN_FRONTEND": "noninteractive",
    }
    assert erratas_de_entorno(ajenas, CAMPOS) == []


def test_una_errata_en_el_entorno_se_caza_aunque_no_haya_env():
    """El agujero que dejaba `forbid`: en Docker no hay `.env` que mirar."""
    erratas = erratas_de_entorno({"DRY_RUM": "true"}, CAMPOS)
    assert erratas == [("DRY_RUM", "DRY_RUN")]


@pytest.mark.parametrize(
    "mal,bien",
    [
        ("DRYRUN", "DRY_RUN"),
        ("LOGLEVEL", "LOG_LEVEL"),
        ("CONFIGPATH", "CONFIG_PATH"),
        ("DATABASE_URI", "DATABASE_URL"),
        ("SCHEDULER_ENABLE", "SCHEDULER_ENABLED"),
    ],
)
def test_las_erratas_que_de_verdad_se_cometen(mal, bien):
    assert erratas_de_entorno({mal: "x"}, CAMPOS) == [(mal, bien)]


@pytest.mark.parametrize(
    "nombre",
    ["HEVY_KEY", "HEVY_TOKEN", "HEVY_API_TOKEN", "GARMIN_USER", "GARMIN_USERNAME",
     "GARMIN_PASS", "GARMIN_TOKENS", "TELEGRAM_CHANNEL"],
)
def test_un_ajuste_nuestro_con_otro_nombre_tampoco_pasa(nombre):
    """No son erratas de tecleo, son nombres equivocados, y `difflib` no llega
    a ninguno de estos: `GARMIN_USER` no se parece a `GARMIN_EMAIL` lo bastante.

    Los caza el prefijo, y el prefijo basta como prueba porque en este
    contenedor no vive ningún otro programa que use variables `GARMIN_`,
    `HEVY_` o `TELEGRAM_`.

    La lista está elegida a propósito entre los que el parecido NO caza: con
    `HEVY_APIKEY` o `TELEGRAM_TOKEN` este test pasaría igual con la regla del
    prefijo desconectada, y entonces no estaría comprobando nada.
    """
    import difflib

    from app.settings import PARECIDO_MINIMO

    conocidos = sorted(c.upper() for c in CAMPOS)
    assert not difflib.get_close_matches(nombre, conocidos, n=1, cutoff=PARECIDO_MINIMO), (
        f"{nombre} sí lo caza el parecido; para probar el prefijo hace falta "
        f"uno que no"
    )

    erratas = dict(erratas_de_entorno({nombre: "x"}, CAMPOS))
    assert nombre in erratas
    assert erratas[nombre].startswith(nombre.split("_")[0])


def test_los_ajustes_de_verdad_no_se_denuncian_a_si_mismos():
    entorno = {c.upper(): "x" for c in CAMPOS}
    assert erratas_de_entorno(entorno, CAMPOS) == []


def test_el_error_dice_qué_hacer_no_solo_que_algo_está_mal(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHATID", "123")
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None)
    msg = str(exc.value)
    assert "TELEGRAM_CHATID" in msg
    assert "TELEGRAM_CHAT_ID" in msg, "hay que decir cuál era la buena"
    assert "por defecto" in msg, "y por qué importa que no se aplicara"


# ---------------------------------------------------------------------------
# `.env.example` como documentación
# ---------------------------------------------------------------------------


def _claves_del_ejemplo() -> set[str]:
    """Las claves que nombra el ejemplo, activas o comentadas.

    Las opcionales van comentadas a propósito (`# HEVY_API_BASE=...`): así se
    documentan sin que copiar el fichero cambie ningún valor por defecto.
    """
    claves: set[str] = set()
    for linea in EJEMPLO.read_text(encoding="utf-8").splitlines():
        t = linea.strip().lstrip("#").strip()
        if "=" in t:
            k = t.split("=", 1)[0].strip()
            if k.isupper() and k.replace("_", "").isalnum():
                claves.add(k)
    return claves


def test_el_ejemplo_nombra_todos_los_ajustes_que_existen():
    """Era la única lista de qué se puede configurar, y le faltaban cuatro.

    Un ajuste que existe y no está documentado no es un ajuste, es un secreto:
    `DATABASE_URL` decide si el histórico sobrevive a un `docker compose up`, y
    no había forma de saber que se podía mover.
    """
    faltan = {c.upper() for c in CAMPOS} - _claves_del_ejemplo()
    assert not faltan, f".env.example no menciona: {sorted(faltan)}"


def test_el_ejemplo_no_inventa_ajustes_que_no_existen():
    """Al revés: una clave en el ejemplo que ya no exista ahora impide
    arrancar, porque `forbid` la rechaza en cuanto alguien copie el fichero."""
    sobran = _claves_del_ejemplo() - {c.upper() for c in CAMPOS}
    assert not sobran, f".env.example ofrece ajustes inexistentes: {sorted(sobran)}"


def test_el_ejemplo_no_recomienda_lo_contrario_de_lo_que_trae():
    """Traía `DRY_RUN=false` con un comentario encima diciendo que lo que hay
    que poner los primeros días es `true`. Quien copia el fichero y hace caso
    al comentario acaba con lo contrario de lo que ha leído."""
    texto = EJEMPLO.read_text(encoding="utf-8")
    assert "\nDRY_RUN=true" in texto


def test_el_ejemplo_avisa_de_que_quitar_dry_run_no_es_neutral():
    """Ausente no es "sin decidir": es `false`. Es exactamente lo que pasaba en
    el `.env` de verdad."""
    texto = EJEMPLO.read_text(encoding="utf-8")
    assert "quitar la línea NO es" in texto


def test_el_env_de_verdad_dice_lo_que_hace_con_hevy_y_telegram():
    """`.env` no está en git, así que este test se salta donde no exista."""
    real = REPO_ROOT / ".env"
    if not real.exists():
        pytest.skip("no hay .env en esta copia")
    claves = {
        ln.split("=", 1)[0].strip()
        for ln in real.read_text(encoding="utf-8").splitlines()
        if "=" in ln and not ln.strip().startswith("#")
    }
    assert "DRY_RUN" in claves, (
        "sin esta línea el planificador escribe en Hevy y envía Telegram de "
        "verdad desde el primer arranque, sin que nadie lo haya elegido"
    )
