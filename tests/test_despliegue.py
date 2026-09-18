"""El despliegue de verdad: `umbrel/docker-compose.yml` y `.dockerignore`.

POR QUÉ ESTE FICHERO, Y POR QUÉ NO VA EN `test_andamiaje.py`
------------------------------------------------------------
`test_andamiaje.py` ata el montaje TEMPORAL del PC -el Caddy de pruebas y su
compose- y está escrito para desaparecer con él: tiene un plazo dentro y un
test que se pone rojo si el andamiaje se queda a vivir. Lo de aquí es lo
contrario: es el manifiesto que Umbrel va a ejecutar cuando el andamiaje ya no
esté, o sea justo lo que tiene que sobrevivirle.

De aquel fichero sí se hereda el método, que es el que importa: casi nada de lo
que un manifiesto de despliegue afirma se puede comprobar leyéndolo. Lo afirma
sobre OTRA cosa -sobre el puerto en el que escucha la aplicación, sobre dónde
busca su configuración, sobre con qué usuario escribe-, y esas afirmaciones no
se rompen en voz alta. Se rompen el día de la instalación, que es el peor día
posible para descubrirlas.

LO QUE ESTABA SIN ATAR
----------------------
`umbrel/umbrel-app.yml` sí lo leía un test -comparaba su puerto con el del
Caddy-. El compose que define los servicios, no lo abría nadie. Cinco
kilobytes que deciden si la aplicación arranca, si el proxy la encuentra, si
los datos sobreviven a una actualización y con qué usuario se escribe la base
de datos, y ni una línea de la batería los miraba. La misma forma que tenía
`static/styles.css` antes de `tests/test_estilos.py`: se versiona, se
despliega, y nadie lo abre.

Los modos de fallo que esto persigue, por orden de lo caro que sale cada uno:

  - `APP_HOST` mal. Umbrel compone el nombre del contenedor como
    `<id>_<servicio>_1`. Si no coincide, el proxy no encuentra nada y la
    aplicación sale EN BLANCO, sin un error que diga por qué.
  - El volumen de datos montado donde no toca. Este es el peor, y es el único
    que no se nota el primer día: la aplicación arranca, funciona, guarda su
    base de datos dentro del contenedor, y la primera actualización se lleva
    por delante el histórico entero y las copias de las rutinas de Hevy, que
    son lo único que permite deshacer una escritura.
  - `config.yaml` montado en otro sitio. `load_config` no lo encuentra y el
    contenedor no levanta.
  - El UID. Con otro, el contenedor arranca y luego no puede escribir.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib

import pytest
import yaml

from app.settings import REPO_ROOT, Settings

COMPOSE_UMBREL = REPO_ROOT / "umbrel" / "docker-compose.yml"
MANIFIESTO_UMBREL = REPO_ROOT / "umbrel" / "umbrel-app.yml"
COMPOSE_BASE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _yaml(ruta):
    return yaml.safe_load(ruta.read_text(encoding="utf-8"))


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lo que la imagen es de verdad, preguntándoselo al Dockerfile
# ---------------------------------------------------------------------------


def _directorio_de_trabajo() -> str:
    """El último `WORKDIR` de la etapa final. Es la raíz de todas las rutas."""
    todos = re.findall(r"^\s*WORKDIR\s+(\S+)\s*$", _dockerfile(), re.M)
    assert todos, "el Dockerfile ya no declara ningún WORKDIR"
    return todos[-1].rstrip("/")


def _puerto_de_la_imagen() -> int:
    """El puerto del `CMD`, que es el que se abre de verdad.

    Se lee del `CMD` y no del `EXPOSE` a propósito: `EXPOSE` es documentación
    -no abre nada- y podría quedarse en un número viejo sin consecuencias
    dentro del contenedor. El que decide es el `--port` de uvicorn.
    """
    m = re.search(r'CMD\s*\[[^\]]*"--port"\s*,\s*"(\d+)"', _dockerfile())
    assert m, "no encuentro el `--port` del CMD en el Dockerfile"
    return int(m.group(1))


def _uid_de_la_imagen() -> int:
    m = re.search(r"useradd\s+--uid\s+(\d+)", _dockerfile())
    assert m, "el Dockerfile ya no crea el usuario con un UID explícito"
    return int(m.group(1))


def _ruta_dentro(p) -> str:
    """Una ruta de `Settings` traducida a dónde cae dentro del contenedor.

    `settings.py` las construye contra `REPO_ROOT`, que dentro de la imagen es
    el `WORKDIR`. Traducirlas así -y no escribir `/app/...` a mano aquí- es lo
    que hace que mover una carpeta en el código ponga rojo el montaje que ya no
    le corresponde.
    """
    return f"{_directorio_de_trabajo()}/{p.resolve().relative_to(REPO_ROOT).as_posix()}"


def _montajes(servicio: dict) -> dict[str, tuple[str, str]]:
    """`origen:destino:modo` -> {destino: (origen, modo)}."""
    fuera = {}
    for v in servicio.get("volumes", []):
        partes = str(v).split(":")
        # `C:\...` en Windows no aparece aquí -estas rutas son del contenedor-,
        # así que partir por `:` es seguro.
        origen, destino = partes[0], partes[1]
        modo = partes[2] if len(partes) > 2 else "rw"
        fuera[destino] = (origen, modo)
    return fuera


@pytest.fixture(scope="module")
def umbrel() -> dict:
    return _yaml(COMPOSE_UMBREL)


@pytest.fixture(scope="module")
def servidor(umbrel) -> dict:
    servicios = umbrel["services"]
    assert "server" in servicios, (
        f"el compose de Umbrel ya no tiene un servicio `server`: {sorted(servicios)}. "
        f"Si se ha renombrado, hay que cambiar también `APP_HOST` del proxy, "
        f"que lleva el nombre del servicio dentro"
    )
    return servicios["server"]


# ---------------------------------------------------------------------------
# El proxy tiene que encontrar la aplicación
# ---------------------------------------------------------------------------


def test_el_proxy_de_umbrel_nombra_al_contenedor_como_umbrel_lo_va_a_llamar(umbrel):
    """`APP_HOST` es un nombre compuesto a mano, y si falla no hay error.

    Umbrel bautiza el contenedor como `<id-de-la-aplicación>_<servicio>_1`. Las
    tres piezas viven en sitios distintos: el `id` en `umbrel-app.yml`, el
    nombre del servicio en la clave de este mismo compose, y el `_1` es de
    compose. Renombrar el servicio -o cambiar el `id` al publicar- deja el
    proxy apuntando a una máquina que no existe, y eso en Umbrel no es un 502
    con su mensaje: es la aplicación EN BLANCO.
    """
    ident = _yaml(MANIFIESTO_UMBREL)["id"]
    servicios = [s for s in umbrel["services"] if s != "app_proxy"]
    assert len(servicios) == 1, (
        f"hay más de un servicio además del proxy ({servicios}): este test da "
        f"por hecho que el proxy apunta al único que hay"
    )

    esperado = f"{ident}_{servicios[0]}_1"
    escrito = umbrel["services"]["app_proxy"]["environment"]["APP_HOST"]
    assert escrito == esperado, (
        f"`APP_HOST` dice {escrito!r} y Umbrel va a llamar al contenedor "
        f"{esperado!r} (id {ident!r} + servicio {servicios[0]!r}). El proxy no "
        f"encontrará nada y la aplicación sale en blanco"
    )


def test_el_proxy_de_umbrel_apunta_al_puerto_que_la_imagen_abre(umbrel):
    """`APP_PORT` contra el `--port` del `CMD`, no contra un número escrito."""
    escrito = int(umbrel["services"]["app_proxy"]["environment"]["APP_PORT"])
    real = _puerto_de_la_imagen()
    assert escrito == real, (
        f"el proxy de Umbrel va al {escrito} y el contenedor escucha en el "
        f"{real} (`CMD` del Dockerfile)"
    )


def test_el_manifiesto_y_el_compose_hablan_del_mismo_puerto_de_fuera(umbrel):
    """El `port` de `umbrel-app.yml` es el de FUERA, y no tiene por qué ser el
    de dentro: entre los dos está el `app_proxy`.

    Se comprueba que sean distintos a propósito, porque igualarlos por
    descuido -poner 8000 en el manifiesto- publicaría en el puerto del
    contenedor y es la clase de coincidencia que parece deliberada.
    """
    fuera = _yaml(MANIFIESTO_UMBREL)["port"]
    dentro = int(umbrel["services"]["app_proxy"]["environment"]["APP_PORT"])
    assert fuera != dentro, (
        f"`umbrel-app.yml` reserva el {fuera} y el proxy manda al {dentro}: si "
        f"son el mismo, o sobra el proxy o falta un puerto"
    )


# ---------------------------------------------------------------------------
# Que no haya una segunda puerta
# ---------------------------------------------------------------------------


def test_el_compose_de_umbrel_no_publica_ningun_puerto(servidor):
    """Es la mitad entera de la seguridad de este despliegue.

    En Umbrel el `app_proxy` pide login. Publicar el 8000 además de eso abre
    una puerta SIN login al lado de la que sí lo tiene, y por esa puerta se
    llega a `/api/export` -el histórico completo: sueño, HRV, RPE y el registro
    de la lumbar- y a `POST /api/checkin`, que no solo lee: crea un check-in y
    dispara una decisión.

    Y no se notaría, porque por la puerta buena se entraría igual de bien. Es
    el mismo argumento que `test_el_8000_no_sale_del_bucle_local` hace sobre el
    compose de pruebas, aquí sobre el que va a correr de verdad.
    """
    assert "ports" not in servidor, (
        f"el compose de Umbrel publica {servidor['ports']!r}. En Umbrel el "
        f"acceso va por el `app_proxy`, que es lo único que pide login"
    )


def test_el_compose_de_umbrel_no_construye_nada(servidor):
    """Umbrel instala, no compila: rechaza `build:`.

    El comentario del fichero lo dice, y un comentario no falla. Si alguien
    copia el compose de la raíz para arreglar algo, se trae el `build:` dentro
    y el fallo aparece en la instalación de otra persona.
    """
    assert "build" not in servidor, (
        "el compose de Umbrel lleva `build:`. Umbrel no compila: necesita una "
        "imagen ya publicada"
    )
    assert "image" in servidor, "el servicio no dice de qué imagen sale"


# ---------------------------------------------------------------------------
# Los montajes, que es donde se pierde el histórico sin enterarse
# ---------------------------------------------------------------------------


def test_los_datos_se_montan_donde_la_aplicacion_los_escribe(servidor):
    """El fallo caro y silencioso: la aplicación funciona y no persiste nada.

    Si el destino no es la carpeta donde `settings.py` pone la base de datos,
    el contenedor arranca, el formulario va, el histórico se guarda... dentro
    de la capa de escritura del contenedor. La primera actualización de la
    aplicación se lo lleva todo: el histórico, los tokens de Garmin y las
    copias de las rutinas de Hevy, que son lo único con lo que se puede
    deshacer una escritura.

    El destino se calcula desde `Settings`, no se escribe aquí: mover `data/`
    en el código tiene que poner rojo el montaje que se quedó atrás.
    """
    s = Settings()
    ruta_db = re.sub(r"^sqlite(\+\w+)?:///", "", s.database_url)
    carpeta = _ruta_dentro(type(s.garmin_token_dir)(ruta_db).parent)

    montajes = _montajes(servidor)
    assert carpeta in montajes, (
        f"nada se monta en {carpeta}, que es donde la aplicación escribe su "
        f"base de datos. Monta: {sorted(montajes)}. Sin ese volumen los datos "
        f"viven dentro del contenedor y la primera actualización los borra"
    )

    origen, modo = montajes[carpeta]
    assert "${APP_DATA_DIR}" in origen, (
        f"{carpeta} se monta desde {origen!r}. En Umbrel tiene que colgar de "
        f"`${{APP_DATA_DIR}}`, que es lo único que el marco respalda y conserva "
        f"entre actualizaciones"
    )
    assert "ro" not in modo.split(","), (
        f"{carpeta} está montado en solo lectura: ahí se escribe la base de datos"
    )

    # Los tokens de Garmin caen dentro de esa misma carpeta, y si alguien los
    # muda a otro sitio hay que acordarse de montarlo. Aquí se entera.
    tokens = _ruta_dentro(s.garmin_token_dir)
    assert tokens.startswith(carpeta + "/") or tokens == carpeta, (
        f"los tokens de Garmin van a {tokens}, que ya no está dentro del único "
        f"volumen montado ({carpeta}). Cada actualización obligaría a volver a "
        f"autenticarse contra Garmin"
    )


def test_la_configuracion_se_monta_donde_se_lee_y_en_solo_lectura(servidor):
    """`config.yaml` es un FICHERO montado, y eso tiene dos trampas.

    La primera: si el destino no es el que `settings.py` lee, `load_config` no
    encuentra el YAML y el contenedor no levanta. Al menos esa falla fuerte.

    La segunda es la de solo lectura, y es de otro tipo: la aplicación no
    escribe ese fichero nunca, así que montarlo `rw` no rompe nada hoy. Lo que
    hace es dejar abierta la puerta para que algo lo escriba mañana, y este
    fichero es la ÚNICA copia de las reglas de entrenamiento que hay en el
    Umbrel -no se reconstruye desde la imagen, se edita a mano-.
    """
    destino = _ruta_dentro(Settings().config_path)
    montajes = _montajes(servidor)
    assert destino in montajes, (
        f"nada se monta en {destino}, que es de donde `load_config` lee las "
        f"reglas. Monta: {sorted(montajes)}"
    )
    origen, modo = montajes[destino]
    assert "${APP_DATA_DIR}" in origen, (
        f"{destino} se monta desde {origen!r} y tiene que colgar de "
        f"`${{APP_DATA_DIR}}` para sobrevivir a las actualizaciones"
    )
    assert "ro" in modo.split(","), (
        f"{destino} se monta como {modo!r}. La aplicación no lo escribe nunca, "
        f"y es la única copia de las reglas que hay en el Umbrel"
    )


def test_el_usuario_del_compose_es_el_que_la_imagen_sabe_ser(servidor):
    """Con otro UID el contenedor arranca y luego no puede escribir.

    El `user:` del compose y el `useradd --uid` del Dockerfile son el mismo
    número escrito dos veces, y el fallo de que no coincidan no sale al
    arrancar: sale al primer intento de escribir la base de datos, o sea a la
    primera mañana.
    """
    escrito = str(servidor["user"]).split(":")[0]
    assert int(escrito) == _uid_de_la_imagen(), (
        f"el compose corre como UID {escrito} y la imagen crea el usuario con "
        f"el {_uid_de_la_imagen()} (`useradd` del Dockerfile)"
    )


def test_el_healthcheck_de_umbrel_mira_el_mismo_sitio_que_el_de_la_imagen(servidor):
    """Dos healthchecks con la misma URL escrita dos veces.

    El de Umbrel manda -el del compose pisa al de la imagen-, así que si el de
    aquí se queda en un puerto viejo, Umbrel da la aplicación por muerta y la
    reinicia en bucle mientras por dentro está perfectamente.
    """
    prueba = " ".join(str(x) for x in servidor["healthcheck"]["test"])
    puerto = _puerto_de_la_imagen()
    assert f"127.0.0.1:{puerto}" in prueba, (
        f"el healthcheck del compose no llama al puerto {puerto}, que es donde "
        f"escucha el contenedor: {prueba}"
    )

    ruta = re.search(r"https?://[^/]+(/\S*?)['\"]", prueba)
    assert ruta, f"no consigo leer la ruta del healthcheck: {prueba}"
    from app.api import app

    caminos = {getattr(r, "path", "") for r in app.routes}
    assert ruta.group(1) in caminos, (
        f"el healthcheck pide {ruta.group(1)} y la aplicación no sirve esa "
        f"ruta. Umbrel reiniciaría el contenedor en bucle"
    )


def test_la_zona_horaria_del_contenedor_es_la_de_las_reglas(servidor):
    """El comentario del compose dice que esta `TZ` es «para que los logs
    coincidan» con la zona de verdad, que está en `config.yaml`.

    Mientras coincidan, esa frase es cierta y la `TZ` es cosmética. El día que
    dejen de coincidir se vuelve lo contrario de cosmética: los trabajos son a
    las 06:30, 09:00 y 22:30 hora local, y el día del check-in es el del reloj
    del servidor.
    """
    del_compose = servidor["environment"]["TZ"]
    de_las_reglas = _yaml(REPO_ROOT / "config.yaml")["timezone"]
    assert del_compose == de_las_reglas, (
        f"el contenedor de Umbrel va en {del_compose} y `config.yaml` decide "
        f"en {de_las_reglas}"
    )


def test_los_secretos_se_pasan_por_fichero_y_con_ruta_absoluta(servidor):
    """Umbrel invoca compose con varios `--file`, y el primero no está en el
    directorio de la aplicación: una ruta relativa no se resuelve donde uno
    cree, y el `.env` no se lee. La aplicación arranca sin credenciales.
    """
    entradas = servidor["env_file"]
    assert entradas, "el servicio ya no lee ningún `env_file`"
    for e in entradas:
        ruta = e["path"] if isinstance(e, dict) else str(e)
        assert ruta.startswith("${APP_DATA_DIR}"), (
            f"`env_file` usa {ruta!r}. Tiene que ser absoluta y colgar de "
            f"`${{APP_DATA_DIR}}`, o Umbrel la buscará donde no está"
        )


def test_la_version_de_la_imagen_es_la_que_declara_el_proyecto(servidor):
    """La etiqueta está escrita en cuatro sitios y aquí es donde se instala.

    `pyproject.toml`, el compose de la raíz, el manifiesto de Umbrel y este
    fichero. El `Dockerfile` ya explica por qué la etiqueta está congelada -para
    eso existe `BUILD_SHA`-, así que el riesgo no es que se quede quieta: es
    que se mueva en tres sitios y no en el cuarto, y entonces Umbrel instala
    una imagen que no es la que el proyecto cree estar publicando.
    """
    declarada = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    _, _, etiqueta = str(servidor["image"]).rpartition(":")
    assert etiqueta == declarada, (
        f"el compose de Umbrel instala la etiqueta {etiqueta!r} y el proyecto "
        f"declara la versión {declarada!r} en pyproject.toml"
    )
    assert str(_yaml(MANIFIESTO_UMBREL)["version"]) == declarada, (
        f"`umbrel-app.yml` anuncia otra versión que pyproject.toml ({declarada!r})"
    )
    _, _, base = str(_yaml(COMPOSE_BASE)["services"]["app"]["image"]).rpartition(":")
    assert base == declarada, (
        f"el compose de la raíz construye la etiqueta {base!r} y el proyecto "
        f"declara {declarada!r}"
    )


# ---------------------------------------------------------------------------
# `.dockerignore`: lo que entra y lo que no puede entrar
# ---------------------------------------------------------------------------


def _patrones() -> list[str]:
    return [
        ln.strip()
        for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _casa(patron: str, ruta: str) -> bool:
    """¿Este patrón alcanza esta ruta, o alguna carpeta por encima?

    Es una aproximación a lo que hace Docker, y conviene decir en qué: aquí se
    compara con `fnmatchcase` -sensible a mayúsculas, que en Windows es lo que
    hay que forzar- contra la ruta y contra cada uno de sus directorios padre,
    que es lo que hace que `data/` alcance `data/app.db`. No se implementa
    `**`; si algún día hace falta uno, este ayudante se queda corto y más vale
    que se note aquí que en una imagen.
    """
    partes = ruta.strip("/").split("/")
    return any(
        fnmatch.fnmatchcase("/".join(partes[:i]), patron)
        for i in range(1, len(partes) + 1)
    )


def _excluido(ruta: str) -> bool:
    """Gana el ÚLTIMO patrón que case, que es la regla de Docker."""
    decision = False
    for patron in _patrones():
        negado = patron.startswith("!")
        if _casa(patron.lstrip("!").strip("/"), ruta):
            decision = not negado
    return decision


def _lo_que_copia_el_dockerfile() -> list[str]:
    """Los orígenes de cada `COPY`, saltándose los que vienen de otra etapa."""
    fuera = []
    for linea in re.findall(r"^\s*COPY\s+(.+)$", _dockerfile(), re.M):
        if "--from=" in linea:
            continue
        piezas = [p for p in linea.split() if not p.startswith("--")]
        fuera.extend(p.rstrip("/") for p in piezas[:-1])   # el último es el destino
    return fuera


def test_lo_que_el_dockerfile_copia_no_esta_excluido_del_contexto():
    """El fallo que ya pasó una vez, y salió a la luz de casualidad.

    `.dockerignore` y `Dockerfile` son dos listas que tienen que encajar y que
    nadie compara. Excluir por descuido algo que se copia no da un error claro:
    da un `COPY` que no encuentra nada, o peor, una imagen a la que le falta
    media aplicación y que arranca igual hasta que se pide la parte que falta.
    """
    for origen in _lo_que_copia_el_dockerfile():
        assert not _excluido(origen), (
            f"el Dockerfile hace `COPY {origen}` y `.dockerignore` lo deja "
            f"fuera del contexto. Eso no entra en la imagen"
        )


def test_los_secretos_y_los_datos_no_pueden_entrar_en_la_imagen():
    """Lo primero que dice `.dockerignore` en su cabecera, comprobado.

    Una imagen se sube a un registro y se queda en las capas para siempre: lo
    que entra ya no se saca. Aquí están las credenciales de Garmin, Hevy y
    Telegram, y el histórico entero con los tokens de sesión.

    Se comprueba aunque hoy el `Dockerfile` copie carpeta por carpeta y no
    pueda arrastrarlos, y por eso mismo: la protección de estas líneas solo se
    cobra el día que alguien escriba un `COPY . .`, y ese día nadie va a venir
    a leer este fichero.
    """
    for peligro in (".env", "data/app.db", "data/garmin_tokens/oauth1.json", "legacy/x.py"):
        assert _excluido(peligro), (
            f"`.dockerignore` deja pasar {peligro!r} al contexto de build. Un "
            f"`COPY . .` lo metería en una capa de la imagen para siempre"
        )
    assert not _excluido(".env.example"), (
        "`.env.example` está excluido y es el inventario de ajustes que "
        "`test_settings.py` mantiene vivo; el `!` que lo repesca se ha perdido"
    )


def test_hoy_no_hay_ningun_copy_de_todo():
    """El contrapeso del test de arriba, y la razón de que aquel valga poco solo.

    Mientras el Dockerfile copie `app/`, `static/` y `config.yaml` uno a uno,
    ningún descuido de `.dockerignore` mete un secreto en la imagen. El día que
    aparezca un `COPY . .` eso deja de ser verdad y la única defensa pasa a ser
    la lista de exclusiones. Que ese cambio sea visible -y obligue a mirar esta
    pareja de tests- es justo lo que se quiere.
    """
    for origen in _lo_que_copia_el_dockerfile():
        assert origen not in (".", "./", "/"), (
            "el Dockerfile ha pasado a copiar el contexto entero. Ahora "
            "`.dockerignore` es lo único que separa el `.env` y `data/` de una "
            "capa de la imagen: repasa `test_los_secretos_y_los_datos_no_"
            "pueden_entrar_en_la_imagen` antes de quitar este test"
        )
