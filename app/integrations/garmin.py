"""Lectura de Garmin Connect: wellness diario y actividades.

Devuelve directamente los tipos del motor (`DayMetrics`, `Ride`) para que
`build_signals` no tenga que saber nada de Garmin. Si algún día se cambia de
fuente, se reescribe este fichero y el motor no se entera.

TRES DECISIONES QUE MERECEN EXPLICACIÓN
---------------------------------------
1. **Los tokens se persisten en disco.** Garmin responde 429 con facilidad y un
   login por ejecución es la forma más rápida de que te capen la cuenta. Con
   `garth` los tokens viven en `GARMIN_TOKEN_DIR` (un volumen en Docker) y el
   login solo ocurre cuando caducan.

2. **Backoff exponencial con tope, y el 429 no se traga.** Si tras los
   reintentos sigue habiendo 429, se propaga. Un sistema que decide con datos a
   medias y no lo dice es peor que uno que falla: el motor ya sabe tratar una
   señal ausente (`skipped`), pero solo si se entera de que falta.

3. **Nunca se inventa un dato.** Si un día no tiene HRV, ese día va con
   `hrv=None`. La media móvil de `build_signals` ya sabe qué hacer con los
   huecos; rellenarlos aquí con el último valor conocido falsearía la línea
   base, que es justo lo que da sentido a los umbrales adaptativos.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from app.engine.signals import DayMetrics, Ride

log = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_BACKOFF = 2.0  # segundos; 2, 4, 8, 16, 32
CYCLING_TYPES = {"cycling", "road_biking", "mountain_biking", "gravel_cycling",
                 "indoor_cycling", "virtual_ride", "cyclocross", "e_bike_fitness"}


class GarminError(RuntimeError):
    """Fallo hablando con Garmin que el llamante debe ver."""


class GarminRateLimited(GarminError):
    """429 persistente. Se distingue del resto para poder decirlo en cabecera.

    Un 429 no es un error cualquiera: significa que los datos frescos no han
    llegado, no que la cuenta esté mal. El llamante necesita distinguirlo para
    poder decidir si sigue con lo que tenga y, sobre todo, para contarlo.
    """


# ---------------------------------------------------------------------------
# Normalización de actividades
# ---------------------------------------------------------------------------
# Vive fuera del cliente a propósito: `data/cache/activities.json` guarda
# exactamente el mismo formato que devuelve la API, y las dos fuentes tienen
# que producir `Ride` idénticos. Si la conversión viviera dentro del cliente,
# la caché acabaría con su propia copia y las dos divergirían en silencio.


def ride_from_activity(act: dict[str, Any]) -> Ride | None:
    """Convierte una actividad cruda de Garmin en `Ride`. None si no es bici."""
    tipo = ((act.get("activityType") or {}).get("typeKey") or "").lower()
    if tipo not in CYCLING_TYPES:
        return None

    stamp = act.get("startTimeLocal") or act.get("startTimeGMT") or ""
    try:
        d = datetime.fromisoformat(str(stamp).replace("Z", "")).date()
    except ValueError as exc:
        # Aquí SÍ se revienta, y la diferencia con el `return None` de arriba
        # es toda la cuestión: ese significa "no es una salida en bici", este
        # significaría "es una salida en bici y la estoy tirando". Devolver
        # None en los dos casos hacía que una salida real desapareciera de
        # `load_3d/7d` sin dejar rastro: menos carga de la que hubo, y verde
        # el día que tocaba ámbar.
        #
        # Y no puede ser un caso rutinario: Garmin siempre manda
        # `startTimeLocal` en ISO. Si un día no se puede leer es que ha
        # cambiado el formato, y entonces no se pierde una salida sino
        # TODAS. Ese es exactamente el fallo que tiene que parar el sistema
        # en vez de degradarlo en silencio hasta que alguien lo note meses
        # después.
        raise GarminError(
            f"actividad de bici {act.get('activityId')} con fecha ilegible: "
            f"{stamp!r}. No se descarta una salida real sin decirlo; si el "
            f"formato de Garmin ha cambiado hay que arreglarlo aquí."
        ) from exc

    zonas = None
    secs = [act.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
    if any(s is not None for s in secs):
        zonas = tuple(float(s or 0.0) for s in secs)

    return Ride(
        date=d,
        duration_s=act.get("duration"),
        distance_m=act.get("distance"),
        zones=zonas,
        training_load=act.get("activityTrainingLoad"),
        aerobic_te=act.get("aerobicTrainingEffect"),
        anaerobic_te=act.get("anaerobicTrainingEffect"),
        is_cycling=True,
        activity_id=act.get("activityId"),
        name=act.get("activityName"),
    )


def rides_from_activities(raw: Sequence[dict[str, Any]]) -> list[Ride]:
    """Filtra y normaliza una lista de actividades crudas."""
    out: list[Ride] = []
    for act in raw or []:
        ride = ride_from_activity(act)
        if ride is not None:
            out.append(ride)
    return out


def _is_rate_limit(exc: Exception) -> bool:
    txt = str(exc).lower()
    return "429" in txt or "too many requests" in txt or "rate" in txt and "limit" in txt


@dataclass(frozen=True)
class RetryPolicy:
    """Cuántas veces insistir ante un 429 y cuánto esperar entre intentos.

    Existe porque `schedule.garmin_retry` llevaba declarado desde el principio en
    el `config.yaml` -3 intentos, esperas de 60/300/900 s- y no lo leía nadie: el
    código reintentaba 5 veces con esperas de 2, 4, 8, 16 y 32 segundos. No era
    una función que faltara, era una CONTRADICCIÓN: el YAML prometía un cuarto de
    hora de margen y el código se rendía en uno.

    Y la diferencia importa justo el día que importa. Un 429 no se quita en dos
    segundos; Garmin limita por IP durante minutos. Con el backoff exponencial
    corto los cinco intentos caben dentro del mismo bloqueo, así que reintentar
    no servía de nada más que para insistirle a una puerta cerrada.

    Sin `backoffs` se vuelve al exponencial de siempre, para que un `config.yaml`
    sin la sección se comporte como antes en vez de quedarse sin reintentos.
    """

    attempts: int = MAX_RETRIES
    backoffs: tuple[float, ...] = ()

    def espera(self, intento: int) -> float:
        """Segundos a esperar DESPUÉS del intento `intento` (1-indexado)."""
        if self.backoffs:
            # El validador exige que la lista cubra todas las esperas, así que
            # este `min` solo protege de un uso directo desde código.
            return float(self.backoffs[min(intento - 1, len(self.backoffs) - 1)])
        return BASE_BACKOFF * (2 ** (intento - 1))


def _retry(
    fn,
    *args,
    what: str = "",
    sink: list[str] | None = None,
    policy: RetryPolicy | None = None,
    **kwargs,
) -> Any:
    """Ejecuta `fn` reintentando solo los 429, con la espera que diga la política.

    `sink` recoge una línea por cada 429 encontrado, incluidos los que luego se
    superan al reintentar. Eso importa: un 429 que se sobrevive sigue siendo un
    aviso de que la IP está limitada, y el informe tiene que poder contarlo en
    vez de presentar el resultado como si la lectura hubiera ido fina.
    """
    pol = policy or RetryPolicy()
    last: Exception | None = None
    for intento in range(1, pol.attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - la librería lanza de todo
            last = exc
            if not _is_rate_limit(exc):
                raise
            etiqueta = what or getattr(fn, "__name__", "?")
            if sink is not None:
                sink.append(f"429 en '{etiqueta}' (intento {intento}/{pol.attempts})")
            # Se espera SOLO entre intentos, nunca después del último: dormir
            # quince minutos para luego rendirse retrasa el aviso sin mejorar
            # nada. Esto es un `if` y no un `break` a propósito. Con el `break`
            # había dos condiciones de parada -el límite del bucle y esta- y la
            # del bucle no decidía nada: cambiarla no alteraba el
            # comportamiento, así que un día podía desincronizarse de la otra
            # sin que ningún test lo notara.
            if intento < pol.attempts:
                delay = pol.espera(intento)
                log.warning(
                    "Garmin 429 en %s (intento %d/%d), espero %.0f s",
                    etiqueta, intento, pol.attempts, delay,
                )
                time.sleep(delay)
    raise GarminRateLimited(
        f"Garmin sigue devolviendo 429 en '{what}' tras {pol.attempts} intentos. "
        f"No se decide con datos a medias: mejor fallar y reintentar más tarde."
    ) from last


@dataclass
class GarminClient:
    """Cliente fino sobre `garminconnect`, con sesión persistida."""

    email: str
    password: str
    token_dir: str
    # La política de reintentos sale del `config.yaml` (`schedule.garmin_retry`).
    # Por defecto, la de siempre: así un cliente construido a mano en un test
    # sigue comportándose igual sin tener que pasarle un config entero.
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    # Training readiness: apagado por defecto porque para esta cuenta siempre
    # vuelve vacío. El porqué largo está en `day_metrics`, donde se usa.
    fetch_readiness: bool = False
    _api: Any = None
    # Cada 429 encontrado, incluidos los superados al reintentar. El informe lo
    # lee para no presentar como lectura limpia algo que costó cinco intentos.
    rate_limit_events: list[str] = field(default_factory=list)
    session_resumed: bool = False
    # Métricas que fallaron al leerse, una línea por (día, métrica).
    #
    # No es lo mismo "esa noche no hubo HRV" que "no se pudo leer el HRV de esa
    # noche": lo primero es un dato, lo segundo es un fallo. Los dos acababan en
    # `hrv=None` y el segundo solo se veía en un `log.debug` que nadie mira.
    # Con la lista, el informe puede decir la diferencia.
    fetch_errors: list[str] = field(default_factory=list)

    def connect(self) -> None:
        try:
            from garminconnect import Garmin
        except ImportError as exc:  # pragma: no cover
            raise GarminError(
                "Falta el paquete `garminconnect`. Instálalo con "
                "`pip install garminconnect`."
            ) from exc

        api = Garmin(self.email, self.password)
        # Primero se intenta reanudar la sesión guardada. Solo si no vale se
        # hace login, que es la llamada que provoca los 429.
        try:
            api.login(self.token_dir)
            self.session_resumed = True
            log.info("Garmin: sesión reanudada desde %s", self.token_dir)
        except Exception:  # noqa: BLE001
            log.info("Garmin: sesión no reutilizable, haciendo login")
            _retry(api.login, what="login", sink=self.rate_limit_events, policy=self.retry)
            try:
                api.garth.dump(self.token_dir)
            except Exception:  # noqa: BLE001 - guardar es best-effort
                log.warning("Garmin: no se pudieron guardar los tokens en %s",
                            self.token_dir)
        self._api = api

    # --- wellness -----------------------------------------------------------

    def _fallo(self, iso: str, metrica: str, exc: Exception) -> None:
        """Anota una métrica que no se pudo leer, y lo dice en el log.

        `warning` y no `debug`: el nivel por defecto no muestra los debug, así
        que la única señal de que la lectura iba mal era invisible tanto en el
        log como en el informe. Una semana leyendo el HRV a medias se veía
        igual que una semana leyéndolo entero.
        """
        self.fetch_errors.append(f"{iso}: no se pudo leer {metrica} ({exc})")
        log.warning("Garmin: sin %s el %s: %s", metrica, iso, exc)

    def _campo(self, iso: str, metrica: str, dentro: Any, clave: str, ruta: str) -> Any:
        """Saca `clave` de `dentro`, separando 'no hay dato' de 'no lo entiendo'.

        Los dos casos acababan en `None` y ninguno de los dos se contaba, pero
        no son la misma cosa ni de lejos:

        - **El contenedor no existe o viene vacío**: eso es un dato. Esa noche
          no hubo medición porque el reloj se quedó en la mesilla. `None` es la
          respuesta correcta y no hay nada que anotar.

        - **El contenedor viene CON contenido pero sin la clave**: Garmin ha
          respondido 200, la petición ha ido bien, y aun así el campo no está
          donde estaba. Casi siempre significa que lo han renombrado.

        El segundo es el que no se puede callar, y no por ser un error sino por
        su forma: no falla un día suelto, falla TODOS a partir de ese. Y el
        síntoma no es un aviso, es que la línea base se queda sin puntos
        suficientes, los umbrales adaptativos dejan de existir y el sistema
        pasa a decidir con las constantes de reserva. Semanas de mañanas
        decididas con menos información de la que había, y ni una línea en el
        informe que lo insinúe.

        Una clave presente con valor nulo SÍ es 'no hubo dato': Garmin manda
        `"restingHeartRate": null` los días que no lo mide, y eso es una
        respuesta, no un silencio.
        """
        if not dentro:
            return None
        if clave not in dentro:
            self.fetch_errors.append(
                f"{iso}: Garmin respondió correctamente pero sin '{clave}' "
                f"dentro de {ruta} ({metrica}). El campo ya no está donde "
                f"estaba: si lo han renombrado esto no falla hoy, falla todos "
                f"los días a partir de hoy, y en silencio"
            )
            log.warning("Garmin: falta '%s' en %s el %s", clave, ruta, iso)
            return None
        return dentro[clave]

    def day_metrics(self, day: date) -> DayMetrics:
        """Una fila de wellness. Los huecos se quedan en None a propósito.

        Además de los números, se llevan las respuestas enteras en
        `DayMetrics.raw`. Llamar cuatro veces para quedarse con cinco escalares
        y descartar el resto sería tirar lo que mañana haga falta: aquí no hay
        caché de wellness -las salidas sí la tienen-, así que lo que no se anote
        en la fila hay que volver a pedírselo a Garmin.

        Son CUATRO llamadas, no cinco: la quinta era training readiness, que
        vuelve vacía siempre para esta cuenta y ahora va apagada
        (`wellness.fetch_readiness`). Cuando está apagada, "readiness" sale en
        `not_requested` para que no cuente como hueco.

        Una respuesta que falló NO deja clave en `raw`. Es la misma distinción
        de siempre: "no vino" y "vino vacío" no son lo mismo, y el que aparezca
        la clave con `{}` dentro es la prueba de que la llamada sí contestó.
        """
        if self._api is None:
            raise GarminError("cliente no conectado: llama a connect() primero")
        iso = day.isoformat()

        hrv = rhr = sleep_min = sleep_score = battery = readiness = None
        crudo: dict[str, Any] = {}
        no_pedidas: list[str] = []

        try:
            data = _retry(self._api.get_hrv_data, iso, what="hrv", sink=self.rate_limit_events, policy=self.retry) or {}
            crudo["hrv"] = data
            summary = self._campo(iso, "hrv", data, "hrvSummary", "la respuesta de HRV")
            hrv = self._campo(iso, "hrv", summary, "lastNightAvg", "hrvSummary")
        except GarminError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fallo(iso, "hrv", exc)

        try:
            stats = _retry(self._api.get_stats, iso, what="stats", sink=self.rate_limit_events, policy=self.retry) or {}
            crudo["stats"] = stats
            rhr = self._campo(iso, "rhr", stats, "restingHeartRate", "la respuesta de stats")
        except GarminError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fallo(iso, "rhr", exc)

        try:
            sleep = _retry(self._api.get_sleep_data, iso, what="sleep", sink=self.rate_limit_events, policy=self.retry) or {}
            crudo["sleep"] = sleep
            dto = self._campo(iso, "sueño", sleep, "dailySleepDTO", "la respuesta de sueño")
            secs = self._campo(iso, "sueño", dto, "sleepTimeSeconds", "dailySleepDTO")
            if secs:
                sleep_min = int(secs) // 60
            # `sleepScores` sí puede faltar de verdad: una siesta o una noche
            # que Garmin no consigue puntuar no traen bloque de puntuación. Por
            # eso este nivel se lee con `.get` y no se anota; los de dentro sí.
            scores = (dto or {}).get("sleepScores") or {}
            overall = self._campo(iso, "sueño", scores, "overall", "sleepScores")
            sleep_score = self._campo(iso, "sueño", overall, "value", "sleepScores.overall")
        except GarminError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fallo(iso, "sueño", exc)

        try:
            bb = _retry(self._api.get_body_battery, iso, iso, what="body_battery", sink=self.rate_limit_events, policy=self.retry)
            crudo["body_battery"] = bb
            if bb:
                niveles = self._campo(
                    iso, "body battery", bb[0], "bodyBatteryValuesArray",
                    "la respuesta de body battery",
                ) or []
                # El valor útil es el de la mañana, no el máximo del día: el
                # sistema decide al levantarse.
                if niveles:
                    if len(niveles[0]) > 1:
                        battery = niveles[0][1]
                    else:
                        # Cada nivel es un par [timestamp, valor]. Si deja de
                        # serlo, `niveles[0][1]` no existe y antes eso era un
                        # None más, indistinguible de un día sin reloj.
                        self.fetch_errors.append(
                            f"{iso}: la serie de body battery no viene en "
                            f"pares [instante, valor] sino como "
                            f"{niveles[0]!r}. Ha cambiado el formato"
                        )
        except GarminError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fallo(iso, "body battery", exc)

        # Training Readiness: el propio resumen de Garmin, 0-100.
        #
        # ESTA LLAMADA VA APAGADA, Y NO POR GUSTO
        # ---------------------------------------
        # Primero estuvo declarada y sin conectar: la columna era NULL todos los
        # días y nadie se enteraba. Se conectó, y resultó que el cable llevaba a
        # una toma sin corriente: `get_training_readiness` devuelve `[]` para
        # esta cuenta TODOS los días, incluido ayer (sondeados -1, -2, -3, y -3 a
        # -175: 9 de 9 vacíos). Training Readiness la calcula el reloj, no el
        # servidor, y el suyo no la calcula.
        #
        # El `if tr:` de antes se tragaba esa lista vacía en silencio, así que el
        # resultado era idéntico al del interruptor sin cable: `readiness=None`
        # sin un solo aviso. Ahora o no se pide, o si se pide la lista vacía se
        # cuenta como lo que es.
        #
        # No se borra la opción porque el día que cambie de reloj vuelve a estar
        # disponible, y el día que se encienda hay que notar si sigue vacía. Se
        # apaga en el `config.yaml` (`wellness.fetch_readiness`), donde se lee.
        #
        # Lo que costaba tenerla encendida: una petición por día de ventana, ocho
        # cada mañana y 180 en el backfill largo, contra un límite que ya ha
        # devuelto 429 alguna vez. Y, peor, dejaba TODAS las filas en
        # `fetch_status='partial'` para siempre, que es la forma de que una marca
        # que existe para avisar de huecos reales deje de avisar de nada.
        if not self.fetch_readiness:
            no_pedidas.append("readiness")
        else:
            try:
                tr = _retry(self._api.get_training_readiness, iso, what="readiness", sink=self.rate_limit_events, policy=self.retry)
                crudo["readiness"] = tr
                if tr:
                    primero = tr[0] if isinstance(tr, list) else tr
                    readiness = self._campo(
                        iso, "readiness", primero, "score", "la respuesta de readiness"
                    )
                else:
                    self.fetch_errors.append(
                        f"{iso}: Garmin contestó a training readiness con "
                        f"{tr!r}, o sea con nada. Esta cuenta no tiene esa "
                        f"métrica -la calcula el reloj, y el suyo no-. Se está "
                        f"gastando una petición al día para no traer nada: "
                        f"apágala en wellness.fetch_readiness"
                    )
                    log.warning("Garmin: training readiness vacío el %s", iso)
            except GarminError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._fallo(iso, "readiness", exc)

        return DayMetrics(
            date=day,
            hrv=float(hrv) if hrv is not None else None,
            rhr=float(rhr) if rhr is not None else None,
            sleep_min=sleep_min,
            sleep_score=int(sleep_score) if sleep_score is not None else None,
            body_battery=int(battery) if battery is not None else None,
            readiness=int(readiness) if readiness is not None else None,
            raw=crudo or None,
            not_requested=tuple(no_pedidas),
        )

    # --- actividades --------------------------------------------------------

    def raw_activities(self, start: date, end: date) -> list[dict[str, Any]]:
        """Actividades del rango tal cual las devuelve Garmin, sin normalizar.

        La caché guarda ESTO y no los `Ride` ya parseados: el día que
        `ride_from_activity` aprenda a leer un campo nuevo, un histórico de
        objetos normalizados no lo tendría y habría que volver a bajar 180 días
        de Garmin. Con el crudo guardado se reparsea y ya está.
        """
        if self._api is None:
            raise GarminError("cliente no conectado: llama a connect() primero")

        return _retry(
            self._api.get_activities_by_date,
            start.isoformat(), end.isoformat(),
            what="activities",
            sink=self.rate_limit_events, policy=self.retry,
        ) or []

    def rides(self, start: date, end: date) -> list[Ride]:
        """Salidas en bici del rango, ya normalizadas."""
        return rides_from_activities(self.raw_activities(start, end))

    # --- conveniencia -------------------------------------------------------

    def window(
        self,
        day: date,
        days: int = 7,
        ride_days: int | None = None,
    ) -> tuple[list[DayMetrics], list[Ride]]:
        """Wellness y salidas hasta `day`.

        `days` y `ride_days` son distintos a propósito. El wellness se consulta
        día a día (una llamada por métrica y por día), así que pedir 180 días
        serían cientos de peticiones y un 429 garantizado; con 7 basta, porque
        las líneas base de HRV y FC en reposo usan una ventana de esa escala.

        El histórico de salidas es otra cosa: viene en UNA sola llamada por
        rango, y los umbrales adaptativos (`load_3d_p90`) necesitan 60 días de
        distribución para existir. Pedir solo 7 los deja en None para siempre.
        """
        start = day - timedelta(days=days - 1)
        metrics = [self.day_metrics(start + timedelta(days=i)) for i in range(days)]
        rides_start = day - timedelta(days=(ride_days or days) - 1)
        return metrics, self.rides(min(rides_start, start), day)


def retry_policy_from_config(config: Any = None) -> RetryPolicy:
    """Lee `schedule.garmin_retry`. Sin sección, la política de siempre."""
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    r = ((raw.get("schedule") or {}).get("garmin_retry") or {})
    if not r:
        return RetryPolicy()
    return RetryPolicy(
        attempts=int(r.get("attempts", MAX_RETRIES)),
        backoffs=tuple(float(x) for x in (r.get("backoff_seconds") or [])),
    )


def wellness_config(config: Any = None) -> dict[str, Any]:
    """La sección `wellness` del YAML, acepte un Config o un dict pelado."""
    raw = (config.raw if hasattr(config, "raw") else config) or {}
    return raw.get("wellness") or {}


def build_client(settings: Any, config: Any = None) -> GarminClient:
    if not settings.garmin_email or not settings.garmin_password:
        raise GarminError(
            "Faltan GARMIN_EMAIL y/o GARMIN_PASSWORD. Ponlos en el fichero .env "
            "(no se piden por consola ni se guardan en config.yaml)."
        )
    token_dir = str(settings.garmin_token_dir)
    return GarminClient(
        email=settings.garmin_email,
        password=settings.garmin_password,
        token_dir=token_dir,
        retry=retry_policy_from_config(config),
        fetch_readiness=bool(wellness_config(config).get("fetch_readiness", False)),
    )
