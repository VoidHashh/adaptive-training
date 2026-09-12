"""El andamiaje de la fase de pruebas, que es temporal y no tiene forma de irse.

`Caddyfile.pruebas` y `docker-compose.pruebas-lan.yml` existen para una sola
cosa: abrir el formulario desde el móvil mientras se compara lo que decide el
sistema con lo que uno habría hecho. Los dos empiezan con **TEMPORAL** y con
"borrar al pasar a Umbrel", y eso, tal cual, no vale nada. Un comentario que
certifica su propia caducidad es la misma figura que este proyecto lleva meses
cazando: el validador que daba por buena una sección muerta, la fila de
`workout_log` que decía "Sí", el documento que decía "no implementada" con cinco
vistas sirviendo. En los tres casos la pieza que tenía que avisar era justo la
que decía que no hacía falta.

SE QUEDAN VERSIONADOS, Y ES A PROPÓSITO
---------------------------------------
La pregunta de la limpieza era si estos dos ficheros debían seguir en el
repositorio. Sí, por dos motivos. Llevan dentro dos cosas que costaron tiempo
averiguar -que el bind mount de Windows mata a SQLite en modo WAL, y el
inventario exacto de lo que queda expuesto al servir sin contraseña- y sin
versionar eso lo borra un `git clean` cualquiera. Y tienen que estar en la RAÍZ,
porque Compose resuelve las rutas relativas contra el directorio del primer
`-f`, y meterlos en una carpeta convierte un comando de una línea en una
sutileza que se pisa a las siete de la mañana.

Lo que sí les faltaba es poder fallar. Eso es lo que hay aquí.

LO QUE SE ATA
-------------
Casi todo lo que estos dos ficheros afirman, lo afirman sobre OTRO fichero: que
el 8317 es el que reserva el manifiesto de Umbrel, que la aplicación escucha en
el 8000, que el 8000 no sale del bucle local, que el aviso de arranque dice la
verdad sobre la puerta. Ninguna de esas cuatro cosas se rompe en voz alta. Se
rompen el día que alguien toca el otro lado, y se notan la mañana que uno coge
el móvil y no hay nada al otro lado -o peor, sí lo hay y no es lo que se creía-.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
import yaml

from app.settings import REPO_ROOT, Settings

CADDY = REPO_ROOT / "Caddyfile.pruebas"
COMPOSE_PRUEBAS = REPO_ROOT / "docker-compose.pruebas-lan.yml"
COMPOSE_BASE = REPO_ROOT / "docker-compose.yml"
MANIFIESTO_UMBREL = REPO_ROOT / "umbrel" / "umbrel-app.yml"

# El día que se montó el andamiaje (commit `b889b72`) y lo que se le da de
# plazo. El número no es redondo por casualidad: ocho semanas es lo que el
# propio proyecto exige antes de fiarse de una correlación -está escrito en
# `docs/analisis.md` y en el aviso de muestra insuficiente de la interfaz-, o
# sea lo que dura como mínimo la fase que este andamiaje sirve. El mes de más es
# para hacer la mudanza sin que la suite esté dando la lata en mitad.
INICIO_DE_LA_FASE = date(2026, 9, 11)
PLAZO_DIAS = 120


def _viven() -> list[str]:
    return [f.name for f in (CADDY, COMPOSE_PRUEBAS) if f.exists()]


@pytest.fixture(autouse=True)
def _solo_mientras_exista_el_andamiaje():
    """Cuando el andamiaje se borre, estos tests se apartan solos.

    Es la diferencia entre un guardia y un estorbo. Un guardia que hay que
    desactivar a mano el día que se cumple su propósito acaba desactivado el día
    anterior, por comodidad, y a partir de ahí no guarda nada.
    """
    if not _viven():
        pytest.skip(
            "el andamiaje de pruebas ya no está: la fase terminó y este fichero "
            "de tests sobra con él, se puede borrar"
        )


def _caddy_sin_comentarios() -> str:
    """El Caddyfile con las almohadillas fuera.

    Hace falta y no es remilgo: la cabecera EXPLICA que antes había un
    `basic_auth` y por qué se quitó, así que buscar la palabra en el fichero
    entero encontraría siempre la explicación y nunca la directiva. Es el mismo
    tropiezo que en `tests/test_docs.py`, donde el documento cita la frase vieja
    al contar cómo se rompió: un texto que habla de su propio pasado no se puede
    registrar con una búsqueda literal.
    """
    lineas = []
    for linea in CADDY.read_text(encoding="utf-8").splitlines():
        sin = linea.split("#", 1)[0]
        if sin.strip():
            lineas.append(sin)
    return "\n".join(lineas)


def _yaml(ruta):
    return yaml.safe_load(ruta.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Los puertos: tres ficheros repitiendo el mismo número a mano
# ---------------------------------------------------------------------------


def test_el_puerto_de_pruebas_es_el_que_reserva_umbrel():
    """El 8317 no es un puerto libre cualquiera, y por eso puede caducar.

    El compose de pruebas dice, con esas palabras, que usa el 8317 porque "es el
    mismo puerto que reserva `umbrel/umbrel-app.yml`, así que la dirección que
    uno se aprende ahora es la que seguirá valiendo después". Y el manifiesto de
    Umbrel avisa por su cuenta de que ese puerto tiene que ser único entre TODAS
    las aplicaciones instaladas, o la segunda no arranca.

    O sea que es un número con motivos para cambiar. El día que cambie allí y no
    aquí, el compose seguirá diciendo su frase y la frase será mentira: uno se
    habrá aprendido una dirección que luego no vale, que es exactamente lo
    contrario de lo que se buscaba.
    """
    umbrel = _yaml(MANIFIESTO_UMBREL)["port"]

    pruebas = _yaml(COMPOSE_PRUEBAS)["services"]["proxy"]["ports"]
    publicados = {int(str(p).split(":")[0]) for p in pruebas}

    escucha = {int(p) for p in re.findall(r"^\s*:(\d+)\s*\{", _caddy_sin_comentarios(), re.M)}

    assert publicados == {umbrel}, (
        f"el compose de pruebas publica {sorted(publicados)} y umbrel-app.yml "
        f"reserva el {umbrel}: la dirección que uno se aprende ahora no será la "
        f"de después"
    )
    assert escucha == {umbrel}, (
        f"Caddy escucha en {sorted(escucha)} y el compose publica el {umbrel}: "
        f"el proxy no contestaría"
    )


def test_el_proxy_de_pruebas_apunta_a_donde_la_aplicacion_escucha_de_verdad():
    """`reverse_proxy app:8000` son dos datos del otro compose escritos a mano.

    El nombre del servicio y el puerto de dentro del contenedor. Si el compose
    de la raíz cambia cualquiera de los dos, esto no falla al construir: falla
    con un 502 la mañana que uno abre el móvil, y un 502 no dice cuál de las dos
    mitades se movió.
    """
    destino = re.search(r"reverse_proxy\s+(\S+)", _caddy_sin_comentarios())
    assert destino, "el Caddyfile de pruebas ya no reenvía a ningún sitio"
    servicio, _, puerto = destino.group(1).partition(":")

    base = _yaml(COMPOSE_BASE)["services"]
    assert servicio in base, (
        f"Caddy reenvía a `{servicio}` y en docker-compose.yml los servicios son "
        f"{sorted(base)}"
    )

    dentro = {int(str(p).split(":")[-1]) for p in base[servicio]["ports"]}
    assert int(puerto) in dentro, (
        f"Caddy reenvía al {puerto} y `{servicio}` escucha en {sorted(dentro)}"
    )


def test_el_8000_no_sale_del_bucle_local():
    """Esto es del compose de la raíz, pero es lo que sostiene todo lo demás.

    Toda la cabecera del andamiaje se apoya en una frase: lo único que sale a la
    red es el 8317 del proxy. Si el compose de la raíz publicara el 8000 a
    secas, esa frase sería falsa y el proxy dejaría de ser el camino para
    volverse un camino más -uno de dos, y el otro sin nada delante-.

    Nadie lo notaría, porque por el 8317 se seguiría entrando igual de bien.
    """
    for p in _yaml(COMPOSE_BASE)["services"]["app"]["ports"]:
        assert str(p).startswith("127.0.0.1:"), (
            f"docker-compose.yml publica {p!r} a toda la red: el histórico "
            f"entero (`/api/export`) y el `POST /api/checkin` quedan al alcance "
            f"de cualquiera del wifi, sin proxy delante"
        )


# ---------------------------------------------------------------------------
# Que el log no mienta sobre quién guarda la puerta
# ---------------------------------------------------------------------------


def test_lo_que_el_arranque_dice_de_la_puerta_es_lo_que_hay_en_el_proxy():
    """`AUTH_FRONT` es una declaración a mano sobre un fichero que no se lee.

    La aplicación no puede mirar si hay alguien delante, así que se lo cree.
    Aquí sí se puede mirar, porque el proxy de esta fase está en el repositorio,
    y entonces la declaración deja de ser un acto de fe.

    Las dos direcciones importan, y no por igual. Poner `basic_auth` y olvidarse
    de cambiar el compose deja el log diciendo "sin contraseña, a propósito"
    habiendo una: molesto, pero el error cae del lado bueno. Al revés -quitar el
    `basic_auth` y dejar `AUTH_FRONT=proxy`- deja el log CERTIFICANDO una puerta
    que ya no existe, y ese es el fallo que este ajuste se inventó para que no
    pudiera ocurrir. Es, literalmente, lo que pasó aquí en septiembre: se quitó
    la contraseña; lo que no se quitó fue la declaración, porque se cambió a la
    vez y a mano. La próxima vez puede no coincidir.
    """
    declarado = _yaml(COMPOSE_PRUEBAS)["services"]["app"]["environment"]["AUTH_FRONT"]

    # Que sea un valor que `Settings` acepte, y no uno que parezca aceptable.
    # `app_proxy` -el nombre del componente de Umbrel- es justo lo que uno
    # escribiría de memoria y el validador lo rechaza; si llegara hasta aquí, el
    # contenedor no arrancaría y el motivo estaría en un compose que nadie
    # relee.
    assert Settings(_env_file=None, AUTH_FRONT=declarado).auth_front == declarado

    pide_credenciales = "basic_auth" in _caddy_sin_comentarios()
    esperado = "proxy" if pide_credenciales else "ninguna"
    assert declarado == esperado, (
        f"Caddyfile.pruebas {'pide' if pide_credenciales else 'no pide'} "
        f"credenciales y el compose declara AUTH_FRONT={declarado!r}: el aviso "
        f"de arranque estaría contando otra cosa de la que hay"
    )


# ---------------------------------------------------------------------------
# La caducidad
# ---------------------------------------------------------------------------


def test_el_andamiaje_no_se_ha_quedado_a_vivir():
    """"TEMPORAL" en un comentario no es un plazo: es una intención.

    Y las intenciones en este repositorio han envejecido mal todas las veces. Un
    plazo escrito aquí sí caduca en voz alta, que es la única diferencia que
    importa entre esto y la cabecera del fichero.

    No es una regla moral sobre el desorden. Lo que se queda a vivir es un
    montaje que publica a toda la red de casa, sin contraseña, un servicio con
    `/api/export` y con un `POST` que dispara decisiones. Hoy eso es una
    decisión tomada con los ojos abiertos y para unas semanas. Dentro de un año,
    si nadie lo ha mirado, es la configuración normal de la casa.
    """
    caduca = INICIO_DE_LA_FASE + timedelta(days=PLAZO_DIAS)
    assert date.today() <= caduca, (
        f"el andamiaje de pruebas cumplió el plazo el {caduca} y sigue aquí "
        f"({', '.join(_viven())}).\n"
        f"Si la mudanza a Umbrel ya está hecha: borra esos ficheros, la sección "
        f"TEMPORAL del README y este fichero de tests, que sobra con ellos.\n"
        f"Si no lo está, mueve `INICIO_DE_LA_FASE`/`PLAZO_DIAS` aquí arriba y "
        f"escribe por qué. Lo que no vale es que nadie lo mire."
    )


def test_el_andamiaje_se_va_entero_o_no_se_va():
    """Medio andamio es peor que el andamio: es basura con pinta de montaje.

    Un `Caddyfile.pruebas` sin su compose no lo levanta nadie, pero se lee como
    si describiera algo que está funcionando. El caso contrario es peor: el
    compose sin el Caddyfile monta el proxy con un fichero de configuración que
    no existe, y Caddy arranca con su página por defecto -responde, no reenvía-.
    """
    assert len(_viven()) == 2, (
        f"queda medio andamiaje: {_viven()}. Los dos ficheros se levantan y se "
        f"borran juntos, y así lo dicen sus dos cabeceras"
    )
