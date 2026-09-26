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
# El taller que publica la imagen, y el nombre que nadie comparaba
# ---------------------------------------------------------------------------


TALLER = REPO_ROOT / ".github" / "workflows" / "docker.yml"


def _taller() -> dict:
    return _yaml(TALLER)


def _paso_del_build() -> dict:
    """El paso de `build-push-action`, que es el que publica de verdad."""
    for paso in _taller()["jobs"]["build"]["steps"]:
        if "build-push-action" in str(paso.get("uses", "")):
            return paso
    raise AssertionError(
        "el taller ya no usa `docker/build-push-action`. Si se ha cambiado por "
        "otra cosa, estos tests miran un paso que no existe y hay que "
        "reescribirlos, no borrarlos"
    )


def _repositorio_de_ghcr() -> str:
    """`<duenyo>/<repo>` en minúsculas, sacado del `repo:` del manifiesto.

    De ahí y no de otro sitio porque es lo que el taller va a usar: publica en
    `ghcr.io/${GITHUB_REPOSITORY,,}`, que es exactamente el `<dueño>/<repo>` de
    la URL de este repositorio, en minúsculas. Leerlo del manifiesto tiene
    además un efecto de rebote que vale la pena: hasta hoy `repo:` era un campo
    decorativo -lo escribía el manifiesto y no lo leía nadie-, y un campo que
    nadie lee es un campo que se queda viejo sin que se note.
    """
    url = str(_yaml(MANIFIESTO_UMBREL)["repo"]).rstrip("/")
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?$", url)
    assert m, f"el `repo:` del manifiesto no parece una URL de GitHub: {url!r}"
    return f"{m.group(1)}/{m.group(2)}".lower()


def test_umbrel_instala_la_imagen_que_este_repositorio_publica(servidor):
    """El hueco que encontró el banco de mutaciones (25/09/2026).

    `test_la_version_de_la_imagen_es_la_que_declara_el_proyecto` compara la
    ETIQUETA y tira el resto: hace `rpartition(":")` y se queda con lo de la
    derecha. Cambiar `ghcr.io/voidhashh/adaptive-training` por
    `ghcr.io/otro/adaptive-training` dejaba la batería entera en verde.

    Eso importa desde que la imagen la publica un taller de GitHub Actions, que
    empuja a `ghcr.io/${GITHUB_REPOSITORY,,}` y no a lo que diga este fichero.
    Son dos sitios que deciden el mismo nombre y que nadie comparaba. El fallo
    que sale de ahí no se ve al instalar -Umbrel descarga una imagen que existe
    y arranca-: se ve cuando un arreglo publicado aquí no aparece nunca por allí.
    """
    nombre, _, _ = str(servidor["image"]).rpartition(":")
    esperado = f"ghcr.io/{_repositorio_de_ghcr()}"
    assert nombre == esperado, (
        f"el compose de Umbrel instala {nombre!r} y el taller publica en "
        f"{esperado!r} (de `repo:` del `umbrel-app.yml`). Umbrel se traería una "
        f"imagen que este repositorio no construye"
    )


def test_el_taller_publica_la_etiqueta_que_umbrel_instala():
    """Que la etiqueta exista en el registro, no solo en el compose.

    El compose instala `:<versión de pyproject>`. Si el taller solo publicara
    `:latest` -que es lo primero que uno escribe-, la instalación fallaría al
    hacer `pull` con un «manifest unknown», y en la interfaz de Umbrel eso es un
    error genérico de instalación sin ningún sitio donde leer la causa.
    """
    etiquetas = str(_paso_del_build()["with"]["tags"]).split()
    _, _, version = str(_yaml(COMPOSE_UMBREL)["services"]["server"]["image"]).rpartition(":")
    assert any(e.endswith(f":{version}") or "steps.version.outputs.v" in e for e in etiquetas), (
        f"el compose instala la etiqueta {version!r} y el taller publica "
        f"{etiquetas!r}. Umbrel pediría una etiqueta que no está en el registro"
    )


def test_el_taller_pasa_las_marcas_que_el_dockerfile_declara():
    """Sin esto, `/api/health` miente sobre qué código está corriendo.

    El `Dockerfile` declara `BUILD_SHA` y `BUILD_DATE` y los deja vacíos sin
    protestar -es deliberado: prefiere decir «no se sabe» a inventarse un
    valor-. El precio es que olvidarlos en el taller no rompe nada: la imagen
    construye, arranca, y `/api/health` contesta sin `build`. Y esa es la única
    pregunta que se hace después de CADA arreglo, porque la etiqueta lleva
    congelada desde el primer día y no la puede contestar.
    """
    declarados = set(re.findall(r"^\s*ARG\s+(BUILD_\w+)", _dockerfile(), re.M))
    assert declarados, "el Dockerfile ya no declara ningún `ARG BUILD_*`"
    pasados = {
        linea.split("=", 1)[0].strip()
        for linea in str(_paso_del_build()["with"]["build-args"]).splitlines()
        if "=" in linea
    }
    faltan = declarados - pasados
    assert not faltan, (
        f"el Dockerfile declara {sorted(declarados)} y el taller solo pasa "
        f"{sorted(pasados)}. Sin {sorted(faltan)}, `/api/health` no puede decir "
        f"qué commit está corriendo"
    )


LEEME_UMBREL = REPO_ROOT / "umbrel" / "README.md"


def _rutas_de_app_data(servicio: dict) -> set[str]:
    """Todo lo que el compose espera encontrar dentro de `${APP_DATA_DIR}`."""
    crudo = [str(v) for v in servicio.get("volumes", [])]
    for e in servicio.get("env_file", []):
        crudo.append(e["path"] if isinstance(e, dict) else str(e))
    return {
        m.group(1)
        for m in (re.match(r"\$\{APP_DATA_DIR\}/([^:]+)", c) for c in crudo)
        if m
    }


def test_las_instrucciones_nombran_todo_lo_que_el_compose_espera_encontrar(servidor):
    """La instalación que falla con un error que no dice nada (25/09/2026).

    Umbreld contestó esto en la interfaz, y nada más:

        Command failed with exit code 1: .../app-script install
        planb-adaptive-training

    Debajo: el compose monta `${APP_DATA_DIR}/config.yaml`, ese fichero no
    estaba, y **Docker, ante un origen de bind mount que no existe, crea un
    DIRECTORIO**. `load_config` encontró un directorio donde esperaba un YAML y
    el contenedor murió. El documento de instalación sí lo contaba... y aun así
    falló, porque decía «antes de ABRIR la aplicación» cuando la instalación no
    llega a abrirse.

    Lo que este test ata no es la redacción -eso no se puede comprobar- sino lo
    que sí se puede: que no haya ninguna ruta que el compose necesite y que las
    instrucciones no nombren. Ese es el fallo que se repite, porque añadir un
    montaje es una línea y acordarse del documento es un acto de fe.

    El precio de olvidarlo no es un aviso: es una instalación que se cae con un
    error genérico, en la máquina de otro, y con `${APP_DATA_DIR}` ya sucio para
    que el segundo intento falle igual.
    """
    leeme = LEEME_UMBREL.read_text(encoding="utf-8")
    rutas = _rutas_de_app_data(servidor)
    assert rutas, "el compose ya no monta nada de `${APP_DATA_DIR}`; este test sobra"

    # Se busca `$APP/<ruta>` y no la ruta suelta, y eso lo decidió el banco de
    # mutaciones: con la subcadena a secas, un montaje nuevo de
    # `${APP_DATA_DIR}/secretos` pasaba en verde porque la palabra «secretos»
    # ya salía en una frase del documento. Un test que se conforma con que la
    # palabra aparezca en algún sitio no comprueba nada; el que exige la forma
    # `$APP/<ruta>` exige que haya un COMANDO que cree esa ruta, que es lo que
    # se quiere de unas instrucciones de instalación.
    sin_documentar = sorted(r for r in rutas if f"$APP/{r}" not in leeme)
    assert not sin_documentar, (
        f"el compose espera encontrar {sin_documentar} dentro de "
        f"`${{APP_DATA_DIR}}` y `umbrel/README.md` no trae ningún comando que "
        f"lo cree (se busca la forma `$APP/<ruta>`). Si no está antes de "
        f"instalar, Docker crea un DIRECTORIO con ese nombre y la instalación "
        f"se cae con un error genérico que no dice por qué"
    )


# ---------------------------------------------------------------------------
# El script que mueve la version, que ahora ejecuta un taller sin nadie mirando
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_de_mentira(tmp_path, monkeypatch):
    """Una copia de los cuatro ficheros de la version, y el script apuntando ahi.

    Se trabaja sobre copia y no sobre el repositorio porque estos tests
    ESCRIBEN. Un test que deja `pyproject.toml` con otra version es un test que
    rompe la batería entera a partir del siguiente.
    """
    from scripts import fijar_version

    for lugar in fijar_version.LUGARES:
        destino = tmp_path / lugar.ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(
            (REPO_ROOT / lugar.ruta).read_text(encoding="utf-8"), encoding="utf-8"
        )
    monkeypatch.setattr(fijar_version, "RAIZ", tmp_path)
    return tmp_path


def test_fijar_la_version_la_deja_escrita_en_los_cuatro_sitios(repo_de_mentira):
    """Lo que el taller da por hecho en cada empujon.

    Sin esto, el unico sitio donde se comprobaria que el script funciona seria
    una ejecucion de GitHub Actions, o sea despues de haber publicado.
    """
    from scripts import fijar_version

    fijar_version.fijar("9.8.7")
    assert {v for _, v in fijar_version.leer_versiones()} == {"9.8.7"}


def test_un_patron_que_deja_de_casar_revienta_y_no_escribe_nada(repo_de_mentira):
    """EL FALLO QUE ESTE SCRIPT EXISTE PARA NO TENER (25/09/2026).

    Un `sed` que deja de casar no falla: no cambia nada. Si `pyproject.toml`
    pasara a escribir `version="0.1.0"` sin espacios, el taller terminaria en
    verde, publicaria la imagen con la etiqueta de siempre, y el Umbrel no veria
    ninguna actualizacion que ofrecer. Desde el movil eso se parece exactamente
    a que el arreglo esta puesto.

    Se comprueba ademas que NO escribe a medias: con cuatro ficheros, reventar
    en el tercero dejaria dos movidos y dos quietos, que es peor que no haber
    empezado.
    """
    from scripts import fijar_version

    # SE ROMPE EL ULTIMO DE LA LISTA, Y ESO ES LA MITAD DEL TEST.
    #
    # La primera version rompia `pyproject.toml`, que es el PRIMERO: reventaba
    # antes de escribir nada, asi que la comprobacion de «no escribe a medias»
    # pasaba igual aunque el script escribiera fichero a fichero. El banco de
    # mutaciones lo enseno: quitarle la atomicidad no ponia rojo a nadie.
    # Rompiendo el ultimo, un script no atomico ya ha escrito los tres de antes.
    #
    # Y se rompe con comillas SIMPLES, que YAML admite igual. Quitar los
    # espacios no valia: el patron lleva `\s*` y casa igual sin ellos.
    # Y la version que se rompe se LEE, no se escribe: `0.1.0` estuvo aqui como
    # literal y dejo de casar en el primer empujon del taller, que la subio a
    # `0.1.193`. Con el literal viejo el `replace` no encontraba nada, el
    # fichero no se rompia y el test pasaba en verde sin haber roto nada.
    ultimo = fijar_version.LUGARES[-1]
    actual = dict((l.ruta, v) for l, v in fijar_version.leer_versiones())[ultimo.ruta]
    roto = repo_de_mentira / ultimo.ruta
    texto_roto = roto.read_text(encoding="utf-8").replace(
        f'version: "{actual}"', f"version: '{actual}'"
    )
    assert texto_roto != roto.read_text(encoding="utf-8"), (
        "la mutacion no ha roto nada: este test ya no prueba lo que dice"
    )
    roto.write_text(texto_roto, encoding="utf-8")
    antes = {
        lugar.ruta: (repo_de_mentira / lugar.ruta).read_text(encoding="utf-8")
        for lugar in fijar_version.LUGARES
    }

    with pytest.raises(fijar_version.NoCasa) as e:
        fijar_version.fijar("9.8.7")
    assert ultimo.ruta in str(e.value)

    for ruta, texto in antes.items():
        assert (repo_de_mentira / ruta).read_text(encoding="utf-8") == texto, (
            f"{ruta} se ha escrito aunque la operacion fallo: el script escribe "
            f"a medias y deja el repositorio en una mezcla que nadie pidio"
        )


def test_dos_lugares_en_el_mismo_fichero_no_pasan_en_silencio(repo_de_mentira, monkeypatch):
    """Por que `fijar` vuelve a LEER del disco en vez de fiarse de lo que escribio.

    El dia que alguien anada un quinto sitio que viva en un fichero que ya esta
    en la lista, los dos textos se calculan sobre el ORIGINAL y se escriben en
    orden: el segundo pisa al primero y se lleva su cambio por delante. El
    script habria «escrito» los cinco y el fichero diria la version vieja en uno
    de ellos.

    Eso no lo caza ningun patron -los dos casan perfectamente-, solo lo caza
    preguntarle al disco como quedo. Es la diferencia entre «he escrito» y
    «esta escrito», y el taller se fia de esto para publicar.
    """
    from scripts import fijar_version

    compose = repo_de_mentira / "docker-compose.yml"
    compose.write_text(
        compose.read_text(encoding="utf-8") + "\n# version-espejo: 0.1.0\n",
        encoding="utf-8",
    )
    espejo = fijar_version.Lugar(
        "docker-compose.yml", r"(?m)^(# version-espejo: )\S+", 1, "inventado para este test"
    )
    monkeypatch.setattr(fijar_version, "LUGARES", fijar_version.LUGARES + (espejo,))

    with pytest.raises(fijar_version.NoCasa) as e:
        fijar_version.fijar("9.8.7")
    assert "docker-compose.yml" in str(e.value)


def test_una_version_que_no_es_una_version_no_llega_a_los_ficheros(repo_de_mentira):
    """El taller la construye con `git rev-list --count`.

    En un clon superficial ese comando puede no devolver un numero, y entonces
    lo que llegaria aqui seria `0.1.` -o vacio-. Escribir eso en los cuatro
    ficheros publica una imagen con una etiqueta absurda y deja el repositorio
    declarando una version que no existe.
    """
    from scripts import fijar_version

    antes = {v for _, v in fijar_version.leer_versiones()}
    assert len(antes) == 1 and None not in antes, f"el repo de partida ya no coincide: {antes}"
    for malo in ("0.1.", "", "latest", "v1.2.3", "1.2"):
        with pytest.raises(ValueError):
            fijar_version.fijar(malo)
    assert {v for _, v in fijar_version.leer_versiones()} == antes


def test_el_script_conoce_exactamente_los_sitios_que_este_fichero_vigila():
    """Las dos listas tienen que ser la misma, y viven en ficheros distintos.

    `test_la_version_de_la_imagen_es_la_que_declara_el_proyecto` compara cuatro
    sitios. `scripts/fijar_version.py` mueve cuatro sitios. Si alguien anade un
    quinto a uno de los dos lados y no al otro, el resultado es el de siempre:
    el taller mueve tres, el test exige cuatro, y quien lo arregle con prisa
    tiene delante dos listas que no sabe que se corresponden.
    """
    from scripts import fijar_version

    del_script = {lugar.ruta for lugar in fijar_version.LUGARES}
    vigilados = {
        "pyproject.toml",
        "docker-compose.yml",
        "umbrel/docker-compose.yml",
        "umbrel/umbrel-app.yml",
    }
    assert del_script == vigilados, (
        f"el script mueve {sorted(del_script)} y este fichero vigila "
        f"{sorted(vigilados)}. Son la misma lista escrita dos veces"
    )


# ---------------------------------------------------------------------------
# La version la pone el COMMIT (gancho pre-commit); el taller solo comprueba
# ---------------------------------------------------------------------------
#
# POR QUE SE MOVIO DEL TALLER AL GANCHO (26/09/2026)
# --------------------------------------------------
# El taller fijaba la version y la DEVOLVIA al repositorio en un commit suyo.
# Funcionaba, y el usuario señalo el defecto a la primera: despues de cada push
# la copia local quedaba un commit por detras de la remota. Dos historias que
# tenian que ser la misma y no lo eran. Ahora el commit sale de la maquina ya
# con su version, y el taller se limita a comprobar que subio.


def _con_git_de_mentira(monkeypatch, *, commits: int, head, padre=None):
    """Lo unico que el script le pregunta a git, sin git: cuantos commits hay y
    que version declaran HEAD y su padre."""
    from scripts import fijar_version

    monkeypatch.setattr(fijar_version, "numero_de_commits", lambda: commits)
    monkeypatch.setattr(
        fijar_version, "version_en", lambda c: {"HEAD": head, "HEAD^": padre}.get(c)
    )


def test_la_siguiente_version_es_el_ordinal_del_commit(repo_de_mentira, monkeypatch):
    """Lo normal: HEAD es el commit 10 y declara 0.1.10; el que viene es el 11."""
    from scripts import fijar_version

    fijar_version.fijar("0.1.10")
    _con_git_de_mentira(monkeypatch, commits=10, head=(0, 1, 10))
    assert fijar_version.siguiente() == "0.1.11"


def test_tras_un_amend_la_version_sigue_subiendo(repo_de_mentira, monkeypatch):
    """`git commit --amend` no crea un commit nuevo: el ordinal no se mueve.

    HEAD es el commit 10 pero ya declara 0.1.11 -porque a ese commit ya se le
    hizo un amend antes-. Con el ordinal a secas saldria otra vez 0.1.11, la
    version no subiria, y el taller -con razon- se negaria a publicar. Por eso
    `siguiente` toma el maximo entre el ordinal y «la de HEAD mas uno».
    """
    from scripts import fijar_version

    fijar_version.fijar("0.1.11")
    _con_git_de_mentira(monkeypatch, commits=10, head=(0, 1, 11))
    assert fijar_version.siguiente() == "0.1.12"


def test_una_version_subida_a_mano_se_respeta(repo_de_mentira, monkeypatch):
    """Si en este commit alguien ya puso 0.2.0, el gancho no la pisa con 0.1.11.

    Cambiar la menor es una decision que se toma a proposito -con
    `fijar_version.py 0.2.0`-, y el gancho la deshacia si aplicaba el ordinal
    sin mirar: el arbol declara MAS que HEAD, y eso es la señal de que alguien
    ya ha decidido.
    """
    from scripts import fijar_version

    fijar_version.fijar("0.2.0")
    _con_git_de_mentira(monkeypatch, commits=10, head=(0, 1, 10))
    assert fijar_version.siguiente() == "0.2.0"


def test_verificar_acepta_un_commit_que_sube(repo_de_mentira, monkeypatch):
    from scripts import fijar_version

    fijar_version.fijar("0.1.11")
    _con_git_de_mentira(monkeypatch, commits=11, head=(0, 1, 11), padre=(0, 1, 10))
    assert fijar_version.verificar() == []


def test_verificar_rechaza_un_commit_hecho_sin_el_gancho(repo_de_mentira, monkeypatch):
    """Un clon sin `core.hooksPath`, o un `--no-verify`: el commit sale con la
    version de su padre.

    Si el taller publicara igual, sacaria la etiqueta de siempre y Umbrel no
    veria ninguna actualizacion que ofrecer. Tiene que negarse, y decir que
    hacer: el mensaje lleva el `git config` que activa el gancho.
    """
    from scripts import fijar_version

    fijar_version.fijar("0.1.10")
    _con_git_de_mentira(monkeypatch, commits=11, head=(0, 1, 10), padre=(0, 1, 10))
    problemas = fijar_version.verificar()
    assert problemas, "un commit que no sube la version ha pasado la verificacion"
    assert "gancho" in problemas[0] and "core.hooksPath" in problemas[0]


def test_verificar_rechaza_cuatro_sitios_que_no_coinciden(repo_de_mentira, monkeypatch):
    from scripts import fijar_version

    fijar_version.fijar("0.1.11")
    manifiesto = repo_de_mentira / "umbrel" / "umbrel-app.yml"
    manifiesto.write_text(
        manifiesto.read_text(encoding="utf-8").replace('version: "0.1.11"', 'version: "0.1.10"'),
        encoding="utf-8",
    )
    _con_git_de_mentira(monkeypatch, commits=11, head=(0, 1, 11), padre=(0, 1, 10))
    problemas = fijar_version.verificar()
    assert problemas and "no dicen lo mismo" in problemas[0]


def test_el_ultimo_commit_de_este_repositorio_subio_la_version():
    """La guarda VIVA: contra el repositorio de verdad, no contra uno de mentira.

    Es exactamente lo que el taller va a ejecutar sobre el commit que se
    empuje. Se pone rojo en local en cuanto alguien hace un commit sin el
    gancho -HEAD declara lo mismo que su padre-, que es mejor que enterarse
    cuando el taller se niegue a publicar.

    Necesita git y un HEAD con padre: este repositorio siempre es un clon.
    """
    from scripts import fijar_version

    assert fijar_version.verificar() == []


def test_el_gancho_mete_en_el_commit_los_mismos_sitios_que_el_script_escribe():
    """El gancho hace `git add` de una lista escrita a mano, y el script escribe
    `LUGARES`. Si un dia se añade un quinto sitio al script y no al gancho, el
    commit sale con tres ficheros movidos y uno sin mover -en el arbol, pero
    fuera del commit- y `--verificar` lo caza en el taller, no antes.

    Y sin retorno de carro: `sh` no entiende un `#!/bin/sh` con `\\r` detras y
    el gancho moriria con «bad interpreter» antes de hacer nada. Eso lo fija
    `.gitattributes`, que tambien se comprueba, porque es lo que sobrevive a un
    clon nuevo en Windows con `core.autocrlf=true`.
    """
    from scripts import fijar_version

    # En BYTES: `read_text` traduce CRLF a LF al leer, y la comprobacion del
    # retorno de carro pasaria siempre, tambien con el gancho roto. La misma
    # trampa que ya cazo `feedback_bancos_preservan_bytes`.
    crudo = (REPO_ROOT / ".githooks" / "pre-commit").read_bytes()
    assert b"\r" not in crudo, "el gancho lleva CRLF: `sh` no lo va a ejecutar"
    gancho = crudo.decode("utf-8")
    assert "fijar_version.py --siguiente" in gancho
    linea_add = next(l for l in gancho.splitlines() if l.strip().startswith("git add"))
    for lugar in fijar_version.LUGARES:
        assert lugar.ruta in linea_add, (
            f"el script escribe {lugar.ruta} y el `git add` del gancho no lo mete "
            f"en el commit"
        )
    atributos = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\.githooks/\*\s+text\s+eol=lf", atributos, re.M), (
        "`.gitattributes` ya no fuerza LF en `.githooks/`: en un clon nuevo de "
        "Windows el gancho saldria con CRLF"
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
