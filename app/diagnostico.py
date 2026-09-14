"""Comprobar a mano que los tres cables siguen conectados.

POR QUÉ HACE FALTA UN BOTÓN
---------------------------
Este sistema decide solo a las siete de la mañana y escribe en Hevy sin que
nadie mire. Las tres conexiones de las que depende -Telegram para contarlo,
Garmin para saber cómo has dormido, Hevy para reescribir la rutina- pueden
romperse en silencio: una clave que caduca, una contraseña cambiada, un
identificador de rutina que ya no existe porque la rutina se borró desde el
móvil. Ninguna de esas tres cosas avisa hasta la mañana en que hacía falta.

Comprobarlas cuesta un minuto SI hay un botón, y no se comprueba nunca si hay
que entrar por SSH a ejecutar un script.

LO QUE ESTAS COMPROBACIONES NO HACEN
------------------------------------
No escriben en Hevy. Ni siquiera en seco. Una "prueba de escritura" que
reescribe la rutina para ver si puede es exactamente el tipo de acción que no
se lanza desde un botón con nombre de diagnóstico, porque el día que falle a la
mitad habrá dejado la rutina del lunes en un estado que nadie pidió.

Telegram sí manda un mensaje de verdad, porque ahí el mensaje ES la prueba: un
"parece que podría enviarse" no demuestra nada, y el canal es el del propio
usuario consigo mismo.

CADA COMPROBACIÓN DICE POR QUÉ FALLA, NO SOLO QUE FALLA
-------------------------------------------------------
Es el mismo criterio que `_clientes` en la API: un aviso que nombra su causa se
arregla desde el móvil, y uno que la adivina obliga a entrar por SSH. Así que
cada paso vuelve con su `ok`, su frase y, si ha reventado, el error tal cual.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Cuántos días atrás se mira para demostrar que la lectura funciona. Ayer y
# anteayer, no hoy: el wellness del día en curso puede estar a medias porque el
# reloj todavía no ha sincronizado, y un hueco ahí diría "Garmin no responde"
# cuando lo que pasa es que son las ocho de la mañana.
DIAS_ATRAS_LECTURA = 2

TEXTO_POR_DEFECTO = (
    "Prueba manual desde el panel. Si lees esto, el aviso de las mañanas "
    "tiene por dónde salir."
)


@dataclass
class Paso:
    """Un eslabón de la cadena, con su resultado y su motivo.

    `ok=None` no es lo mismo que `ok=False`: es que el paso NO SE PUDO HACER
    porque el anterior ya había fallado. Distinguirlo importa, porque una lista
    de cinco cruces rojas hace pensar en cinco averías cuando lo que hay es una
    avería y cuatro consecuencias.
    """

    nombre: str
    ok: bool | None
    detalle: str
    error: str | None = None

    def como_dict(self) -> dict[str, Any]:
        return {
            "nombre": self.nombre,
            "ok": self.ok,
            "detalle": self.detalle,
            "error": self.error,
        }


@dataclass
class Resultado:
    servicio: str
    pasos: list[Paso] = field(default_factory=list)

    def paso(self, nombre: str, ok: bool | None, detalle: str, error: str | None = None):
        self.pasos.append(Paso(nombre, ok, detalle, error))
        return self

    @property
    def ok(self) -> bool:
        """Verde solo si TODOS los pasos que se hicieron salieron bien.

        Un paso que no se pudo hacer (`ok is None`) no cuenta como bueno. Si
        contara, una cadena que se corta en el primer eslabón se pintaría verde
        entera, que es la manera más rápida de que un botón de diagnóstico
        mienta.
        """
        return bool(self.pasos) and all(p.ok is True for p in self.pasos)

    def como_dict(self) -> dict[str, Any]:
        fallo = next((p for p in self.pasos if p.ok is False), None)
        return {
            "servicio": self.servicio,
            "ok": self.ok,
            # El resumen es lo único que se lee de un vistazo, así que cuando
            # algo falla lo que sale es el fallo, no un "3 de 5 pasos bien".
            "resumen": (
                f"{self.servicio}: todo correcto"
                if self.ok
                else f"{self.servicio}: {fallo.detalle}"
                if fallo
                else f"{self.servicio}: sin comprobar"
            ),
            "pasos": [p.como_dict() for p in self.pasos],
        }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def probar_telegram(settings: Any, cfg: Any = None, *, texto: str | None = None) -> dict[str, Any]:
    """Manda un mensaje de verdad al canal de siempre.

    Aquí no hay modo seco que valga. `dry_run` comprueba que el texto se puede
    montar, que es lo que ya comprueba el runner todas las mañanas; lo que este
    botón tiene que demostrar es que el mensaje LLEGA, y eso solo lo demuestra
    un mensaje que llega.
    """
    from app.integrations.telegram import build_client

    r = Resultado("Telegram")
    try:
        cliente = build_client(settings, cfg)
    except Exception as exc:  # noqa: BLE001
        r.paso("credenciales", False, "no se pudo montar el cliente", str(exc))
        r.paso("envío", None, "no se intentó: no hay cliente")
        return r.como_dict()

    # `build_client` no valida nada: monta el dataclass con lo que haya, y con
    # el token vacío monta un cliente perfectamente construido que apunta a
    # `https://api.telegram.org/bot/sendMessage`. Decir aquí «token y chat
    # configurados» sin mirarlos era afirmar algo que este paso no había
    # comprobado.
    faltan = [
        n for n, v in (("token", cliente.bot_token), ("chat", cliente.chat_id))
        if not (v or "").strip()
    ]
    if faltan:
        r.paso("credenciales", False, f"falta {' y '.join(faltan)} en la configuración")
        r.paso("envío", None, "no se intentó: faltan credenciales")
        return r.como_dict()

    r.paso("credenciales", True, "token y chat configurados")

    cuerpo = texto or TEXTO_POR_DEFECTO
    try:
        envio = cliente.send(cuerpo)
    except Exception as exc:  # noqa: BLE001
        r.paso("envío", False, "el mensaje no salió", str(exc))
        return r.como_dict()

    # SE LEEN LOS CAMPOS QUE `SendResult` TIENE DE VERDAD, Y SIN RED.
    #
    # Aquí había un `getattr(envio, "ok", True)` con un comentario que lo
    # justificaba diciendo que `SendResult` «no es igual en todas las versiones
    # del cliente». `SendResult` vive en este mismo repositorio, a un import de
    # distancia, y sus campos son `sent`/`parts`/`reason`/`error`/`preview`/
    # `plain_parts`. Nunca ha tenido un `ok`. El valor por defecto del `getattr`
    # era `True`, así que la rama de fallo era inalcanzable y el botón decía
    # «Telegram: todo correcto» en los tres casos en que el mensaje NO llega:
    # con `send_enabled` en false, en `dry_run`, y cuando la API devuelve un
    # error HTTP en todas las partes.
    #
    # Ese defecto es exactamente lo que este módulo existe para no tener. Un
    # diagnóstico que miente en verde es peor que no tener diagnóstico: con el
    # botón en rojo se investiga, y con el botón en verde se descarta Telegram
    # como causa y se busca el fallo donde no está.
    #
    # Así que se leen por nombre y sin `default`. Si algún día `SendResult`
    # cambia de forma, esto tiene que reventar con un `AttributeError` que se
    # arregla en un minuto, no seguir adelante pintando verde.
    if not envio.sent:
        # `reason` explica el caso apagado -`send_enabled` en false, `dry_run`-
        # y `error` el caso roto. Se enseñan los dos porque no son el mismo
        # problema: uno se arregla en el YAML y el otro no.
        r.paso(
            "envío",
            False,
            envio.reason or "el cliente dice que no se envió",
            envio.error,
        )
        return r.como_dict()

    if envio.error:
        # `send` devuelve `sent=enviados > 0`, así que un mensaje largo cuya
        # primera parte sale y cuya segunda revienta vuelve como enviado. Llegó
        # A MEDIAS, y un mensaje de la mañana a medias es el que se lee entero
        # creyendo que estaba entero.
        r.paso(
            "envío",
            False,
            f"el mensaje salió incompleto: {envio.parts} parte(s) de las que tocaban",
            envio.error,
        )
        return r.como_dict()

    detalle = "mensaje enviado"
    if envio.parts > 1:
        detalle += f", partido en {envio.parts} trozos por longitud"
    if envio.plain_parts:
        # No es un fallo -el texto llegó-, pero llegó sin formato porque
        # Telegram rechazó el HTML, y eso se arregla antes de que le pase al
        # mensaje de un lunes.
        detalle += (
            f". {envio.plain_parts} parte(s) fueron SIN formato: Telegram "
            f"rechazó el HTML"
        )
    r.paso("envío", True, detalle)
    return r.como_dict()


# ---------------------------------------------------------------------------
# Hevy
# ---------------------------------------------------------------------------


def probar_hevy(settings: Any, cfg: Any = None) -> dict[str, Any]:
    """Lee, y solo lee. Ni escribe la rutina ni finge escribirla.

    Lo que de verdad aporta esta comprobación no es "¿vale la clave?" -eso se
    sabría el lunes-, sino **¿siguen existiendo las rutinas que el YAML dice que
    va a reescribir?**. Un identificador que apunta a una rutina borrada desde
    el móvil no da la cara hasta la mañana en que toca escribirla, y entonces ya
    es tarde: el mensaje sale con el aviso de escritura fallida y el día se
    queda sin su versión.

    EL TÍTULO SE ENSEÑA Y NO SE COMPRUEBA. Sale para que se pueda reconocer la
    rutina de un vistazo, y nada más. Ya está medido en este proyecto que 4 de
    14 entrenamientos tienen un título que miente sobre su propia rutina, así
    que un título que no coincide con el del YAML no es un fallo: es un título.
    Lo que cuenta es el identificador, que es lo que se pide y lo que se
    reescribe.
    """
    from app.integrations.hevy import build_client

    r = Resultado("Hevy")
    try:
        cliente = build_client(settings, cfg)
    except Exception as exc:  # noqa: BLE001
        r.paso("credenciales", False, "no se pudo montar el cliente", str(exc))
        r.paso("lectura", None, "no se intentó: no hay cliente")
        r.paso("rutinas", None, "no se intentó: no hay cliente")
        return r.como_dict()

    r.paso("credenciales", True, "clave de API configurada")

    desde = date.today() - timedelta(days=30)
    try:
        workouts = cliente.get_workouts(since=desde)
    except Exception as exc:  # noqa: BLE001
        r.paso("lectura", False, "no se pudieron leer los entrenamientos", str(exc))
        r.paso("rutinas", None, "no se intentó: la lectura ya falla")
        return r.como_dict()

    n = len(workouts)
    r.paso(
        "lectura",
        True,
        f"{n} entrenamiento{'' if n == 1 else 's'} en los últimos 30 días"
        + ("" if n else " (la clave funciona: la lista vacía es una respuesta)"),
    )

    rutinas = _rutinas_declaradas(cfg)
    if not rutinas:
        r.paso("rutinas", False, "el config no declara ninguna rutina con identificador")
        return r.como_dict()

    faltan: list[str] = []
    encontradas: list[str] = []
    for clave, rid in rutinas.items():
        try:
            remota = cliente.get_routine(rid)
        except Exception as exc:  # noqa: BLE001
            faltan.append(f"{clave} ({rid}): {exc}")
            continue
        encontradas.append(f"{clave} -> {_titulo_remoto(remota) or 'sin título'}")

    if faltan:
        r.paso(
            "rutinas",
            False,
            f"{len(faltan)} de {len(rutinas)} no se pudieron leer",
            "; ".join(faltan),
        )
    else:
        r.paso(
            "rutinas",
            True,
            f"las {len(rutinas)} rutinas del config existen en Hevy",
            None,
        )
    # Los títulos van aparte y etiquetados como lo que son: para reconocerlas,
    # no para validarlas.
    if encontradas:
        r.paso(
            "títulos (solo informativos)",
            True,
            " | ".join(encontradas),
        )
    return r.como_dict()


def _rutinas_declaradas(cfg: Any) -> dict[str, str]:
    """Los identificadores del config, sin inventarse ninguno si no hay."""
    if cfg is None:
        return {}
    rutinas = getattr(cfg, "routines", None) or {}
    fuera: dict[str, str] = {}
    for clave, v in rutinas.items():
        rid = v.get("hevy_routine_id") if isinstance(v, dict) else getattr(v, "hevy_routine_id", None)
        if rid:
            fuera[str(clave)] = str(rid)
    return fuera


def _titulo_remoto(remota: Any) -> str | None:
    if not isinstance(remota, dict):
        return None
    for clave in ("title", "name"):
        if remota.get(clave):
            return str(remota[clave])
    dentro = remota.get("routine")
    if isinstance(dentro, dict):
        for clave in ("title", "name"):
            if dentro.get(clave):
                return str(dentro[clave])
    return None


# ---------------------------------------------------------------------------
# Garmin
# ---------------------------------------------------------------------------


def probar_garmin(settings: Any, cfg: Any = None) -> dict[str, Any]:
    """Entra y lee un día reciente.

    OJO CON PULSAR ESTO MUCHAS VECES. Garmin limita los intentos de login, y el
    cliente lo sabe: primero intenta reanudar la sesión guardada y solo hace
    login de verdad si no vale. Por eso el resultado dice CUÁL de las dos cosas
    ha pasado. Si dice "sesión reanudada", pulsar otra vez no cuesta nada; si
    dice "login nuevo", conviene no insistir, porque la respuesta a insistir es
    un 429 y entonces la comprobación se convierte en la avería.

    Se lee un día de hace dos, y no el de hoy: el wellness del día en curso
    puede estar a medias porque el reloj todavía no ha sincronizado, y un hueco
    ahí se leería como "Garmin no responde" cuando lo que pasa es que son las
    ocho de la mañana.
    """
    from app.integrations.garmin import build_client

    r = Resultado("Garmin")
    try:
        cliente = build_client(settings, cfg)
    except Exception as exc:  # noqa: BLE001
        r.paso("credenciales", False, "faltan o no valen", str(exc))
        r.paso("sesión", None, "no se intentó: no hay cliente")
        r.paso("lectura", None, "no se intentó: no hay cliente")
        return r.como_dict()

    r.paso("credenciales", True, "usuario y contraseña configurados")

    try:
        cliente.connect()
    except Exception as exc:  # noqa: BLE001
        r.paso("sesión", False, "no se pudo entrar", str(exc))
        r.paso("lectura", None, "no se intentó: no hay sesión")
        return r.como_dict()

    # TRES ESTADOS, TRES FRASES. `None` no se dobla a "login nuevo" porque no es
    # lo mismo saber que hubo login que no haber podido averiguarlo: lo primero
    # es una advertencia, lo segundo es una pregunta abierta sobre la librería.
    reanudada = getattr(cliente, "session_resumed", False)
    if reanudada is None:
        r.paso(
            "sesión",
            True,
            "conectado, pero no se ha podido saber si hubo login "
            "(la librería ha cambiado por dentro); por si acaso, no repetir seguido",
        )
    elif reanudada:
        r.paso(
            "sesión",
            True,
            "sesión reanudada de los tokens guardados (no gasta intentos de login)",
        )
    else:
        r.paso(
            "sesión",
            True,
            "login nuevo: conviene no repetir la prueba seguida, Garmin limita los intentos",
        )

    # Si hubo login, ¿quedaron los tokens escritos? Es el paso que delata el
    # fallo caro: un login que funciona cada vez porque no guarda nada nunca.
    guardados = getattr(cliente, "tokens_guardados", None)
    if guardados is False:
        r.paso(
            "tokens",
            False,
            "el login funcionó pero la sesión no se guardó: cada ejecución "
            "repetirá el login entero y acabará topando con el límite de Garmin",
            f"nada escrito en {getattr(cliente, 'token_dir', '?')}",
        )
    elif guardados is True:
        r.paso("tokens", True, "sesión guardada: la próxima vez no hará falta login")

    dia = date.today() - timedelta(days=DIAS_ATRAS_LECTURA)
    try:
        m = cliente.day_metrics(dia)
    except Exception as exc:  # noqa: BLE001
        r.paso("lectura", False, f"no se pudo leer el {dia.isoformat()}", str(exc))
        return _con_limites(r, cliente)

    campos = {
        "HRV": m.hrv,
        "FC en reposo": m.rhr,
        "sueño (min)": m.sleep_min,
        "nota de sueño": m.sleep_score,
        "Body Battery": m.body_battery,
    }
    hay = [k for k, v in campos.items() if v is not None]
    no_hay = [k for k, v in campos.items() if v is None]

    if not hay:
        # Conectar y no traer nada no es un éxito. Es justo el fallo que este
        # botón busca: la sesión vale y los datos no llegan.
        r.paso(
            "lectura",
            False,
            f"conecta pero el {dia.isoformat()} no trajo ningún dato",
            "los cinco campos vinieron vacíos",
        )
        return _con_limites(r, cliente)

    detalle = f"{dia.isoformat()}: {', '.join(hay)}"
    if no_hay:
        detalle += f" (sin {', '.join(no_hay)})"
    r.paso("lectura", True, detalle)
    return _con_limites(r, cliente)


def _con_limites(r: Resultado, cliente: Any) -> dict[str, Any]:
    """Añade el recuento de 429 y cierra el informe.

    VA AL FINAL Y NO EN MEDIO. Los 429 no solo salen del login: cada lectura de
    wellness puede topar con el límite y reintentar por dentro. Si este paso se
    montara justo después de conectar, contaría los del login y se perdería los
    de la lectura, que son los que convierten una respuesta aparentemente limpia
    en una que ha costado cinco intentos.

    Por eso se llama desde TODAS las salidas posteriores a la conexión, incluida
    la de lectura fallida: precisamente cuando la lectura falla es cuando más
    importa saber si lo que falló fue el límite.
    """
    limites = list(getattr(cliente, "rate_limit_events", []) or [])
    if limites:
        r.paso(
            "límite de Garmin",
            False,
            f"{len(limites)} respuesta{'' if len(limites) == 1 else 's'} 429 "
            "durante la comprobación: conviene esperar un rato antes de insistir",
            " | ".join(str(x) for x in limites[:5]),
        )
    return r.como_dict()
