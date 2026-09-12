# Entrenamiento adaptativo

Cada mañana decide qué versión de la sesión toca, reescribe la rutina de fuerza
en Hevy y manda un mensaje de Telegram con la sesión y la indicación de bici.
Funciona solo, sin que nadie lo mire.

Las decisiones las toma un motor de **reglas explícitas en `config.yaml`**. No
hay ningún modelo de lenguaje por medio: dos días con los mismos datos dan la
misma decisión, y siempre se puede señalar la regla que la tomó.

## Qué hace, y cuándo

| Hora  | Qué pasa |
|-------|----------|
| 06:30 | Refresca la caché de Garmin (sueño, HRV, pulso en reposo, actividades). |
| —     | Cuando se envía el check-in desde el móvil: decide en ese momento. |
| 09:00 | Si a esa hora no hay check-in, decide igualmente y lo dice. |
| 22:30 | Reconcilia lo que de verdad se entrenó y actualiza el estado. |

El check-in son siete deslizadores y un comentario. **Un deslizador que no se
toca no se envía**: el motor tiene un camino para las señales que faltan y ese
camino es mejor que un 5 inventado.

Cada cierto tiempo el mensaje de la mañana añade un bloque **🛠 Toca
recalibrar**. No es un error ni una decisión: es el sistema recordando que
varios umbrales del `config.yaml` se calibraron sobre muestras cortas y que ya
hay datos suficientes para volver a mirarlos. Se cuenta en **días con decisión
guardada**, no en días de calendario —cuatro semanas con el sistema apagado dos
no son cuatro semanas de datos—, y la única forma de callarlo es poner la fecha
de hoy en `program.recalibrado_el`. Si de mirarlo sale que no cambia nada, se
cambia la fecha igual: decidir no tocar, habiendo mirado, también es recalibrar.

## Instalación

Antes de elegir cómo instalarlo hay que saber esto: **la aplicación no tiene
autenticación propia**. Ninguna. `/api/state`, `/api/decision` y `/api/export`
—el CSV con el histórico entero— contestan a quien pregunte. No es un descuido
pendiente de arreglar, es lo que decide cuál de los dos caminos vale para qué.

Como eso no se puede comprobar desde dentro del contenedor —estar detrás de un
proxy con login y estar publicado en crudo se ven exactamente igual—, **el
arranque lo dice en el log**. `AUTH_FRONT=proxy` declara que hay un proxy
delante y calla el aviso; cualquier otra cosa, incluido no poner nada, avisa.
Ver [la tabla más abajo](#este-montaje-va-sin-autenticación-y-es-una-decisión).

**En Umbrel, por su marco de aplicaciones.** Es el camino bueno para el uso
real. El `app_proxy` de Umbrel pone delante su login sin escribir una línea de
código, y es lo único que permite abrir el formulario desde el móvil sin dejar
el historial de entrenamiento y de salud a la vista de cualquiera que esté en el
wifi. Los tres ficheros y el procedimiento entero están en
**[`umbrel/README.md`](umbrel/README.md)**.

**A mano, para desarrollo o para probar.** El `docker-compose.yml` de la raíz
publica el 8000 **solo en el bucle local**, a propósito: se llega desde la
propia máquina o por un túnel SSH, **no desde el móvil**. Si se cambia ese
enlace a `0.0.0.0` para alcanzarlo desde el teléfono, se está publicando el
histórico entero; para eso está el camino de arriba —o, mientras se prueba en el
PC, [el montaje temporal](#temporal-el-móvil-mientras-se-prueba-en-el-pc), que
**también va sin contraseña** y explica allí por qué y qué queda expuesto.

```bash
git clone <este-repo> adaptive-training
cd adaptive-training

cp .env.example .env
nano .env            # las cinco claves; ver abajo

mkdir -p data
sudo chown -R 1000:1000 data    # el contenedor no corre como root

docker compose up -d --build
```

Comprobar que ha arrancado **entero**:

```bash
curl -s localhost:8000/api/health | python3 -m json.tool
```

```json
{
  "status": "ok",
  "secrets_missing": [],
  "dry_run": true,
  "scheduler": {
    "running": true,
    "jobs": {
      "garmin_fetch":      "2026-09-11T06:30:00+02:00",
      "decision_fallback": "2026-09-11T09:00:00+02:00",
      "reconcile":         "2026-09-10T22:30:00+02:00"
    }
  },
  "clock": { "timezone": "Europe/Madrid", "offset": "+02:00", "matches": true }
}
```

Mirar estas tres cosas, no solo el `status`:

- **`secrets_missing` vacío.** Si falta algo, la aplicación arranca igual -para
  poder abrir el formulario y ver qué falta- pero no podrá hacer su trabajo.
- **`scheduler.running` en `true` y los tres trabajos con hora.** Sin
  planificador la aplicación sirve el formulario, contesta `ok` y no decide
  nunca. Desde fuera se parece muchísimo a una semana de descanso.
- **`clock.matches` en `true`.** Es el reloj del proceso comparado con la zona
  del `config.yaml`. Si no coinciden, un check-in enviado de madrugada se guarda
  con la fecha de ayer y a la mañana siguiente se decide como si no lo hubiera
  habido.

Las tres las pinta también la PWA nada más abrirla, en rojo y arriba del
formulario, que es donde de verdad se van a mirar. Este `curl` es para cuando ya
se sabe que algo pasa.

> **No sirve mirar el `+02:00` de las horas de los trabajos.** Parece la
> comprobación natural y no comprueba nada: los disparadores se construyen con
> la zona del `config.yaml`, así que salen en `+02:00` aunque el contenedor esté
> en UTC —o en Tokio—. Está probado levantando la imagen de las tres formas. Por
> eso existe `clock`: era una comprobación que no podía fallar, ocupando el
> sitio de una que sí.

### Las claves

| Variable | De dónde sale |
|----------|---------------|
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | La cuenta de siempre. |
| `HEVY_API_KEY` | Hevy → Ajustes → Developer (necesita Hevy Pro). |
| `TELEGRAM_BOT_TOKEN` | Lo da `@BotFather` al crear el bot. |
| `TELEGRAM_CHAT_ID` | Escribirle al bot y mirar `api.telegram.org/bot<TOKEN>/getUpdates`. |

### Empezar en seco

`DRY_RUN=true` los primeros días. El sistema decide, guarda y registra, pero no
toca Hevy ni manda Telegram. Se quita cuando lo que decida coincida con lo que
uno habría hecho, no antes.

> **El guión del primer día está aparte: [`docs/primer-dia.md`](docs/primer-dia.md).**
> Lo de aquí arriba es montar el sistema, que es un problema resuelto. Aquello es
> qué mirar la primera mañana para saber si lo que ha arrancado está vivo o solo
> lo parece —que no es lo mismo y desde fuera se ven igual—, en qué orden se
> sueltan los dos frenos, y las tres averías que ya han pasado de verdad.

Mientras esté puesto, la PWA lo dice en ámbar al abrirla. Es la razón de que ese
aviso exista: un silencio *a propósito* y una avería se parecen demasiado desde
el móvil, y las dos se leen igual —no llega mensaje—.

### El formulario en el móvil

Instalado en Umbrel: `http://umbrel.local:8317`, el puerto del
`umbrel/umbrel-app.yml`. Pedirá el login de Umbrel, que es el `app_proxy`
haciendo su trabajo.

**No se instalará como aplicación de verdad, y no es un fallo.** "Añadir a
pantalla de inicio" deja un acceso directo, pero el service worker **no se
registra sobre HTTP**: los navegadores solo lo permiten en contexto seguro
(HTTPS, o `localhost`), y Umbrel sirve las aplicaciones en HTTP dentro de la red
local. Lo que eso quita:

- **No hay arranque sin conexión.** Sin red no se abre nada, en vez de abrirse y
  decir que no hay red.
- Todo lo demás —el formulario, el envío, los avisos— funciona igual.

Para el modo aplicación completo hace falta HTTPS por delante (un túnel, o un
proxy con certificado). No lo hay hoy, y el uso normal —abrirlo por la mañana
con wifi— no lo necesita.

### TEMPORAL: el móvil mientras se prueba en el PC

> **Esto sobra en cuanto el sistema viva en Umbrel.** Son dos ficheros
> —`docker-compose.pruebas-lan.yml` y `Caddyfile.pruebas`— y se borran juntos:
> allí el `app_proxy` hace lo mismo y mejor. Existe por una sola razón: poder
> abrir el formulario desde el móvil por la mañana durante los días de comparar
> lo que decide el sistema con lo que uno habría hecho.
>
> **Y tiene fecha, no solo intención.** «Temporal» escrito en una cabecera no
> caduca nunca; el plazo está en `tests/test_andamiaje.py` y la suite se pone en
> rojo cuando se cumple, diciendo qué borrar. Ese mismo fichero ata las cuatro
> cosas que estos dos ficheros afirman sobre otros —el puerto que reserva
> Umbrel, dónde escucha la aplicación, que el 8000 no sale del bucle local, y
> que `AUTH_FRONT` diga lo que el proxy hace de verdad—, porque ninguna de las
> cuatro se rompe en voz alta.

#### Este montaje va SIN AUTENTICACIÓN, y es una decisión

No hay usuario ni contraseña. Se entra escribiendo la dirección. **Es
deliberado**, no un cabo suelto: es la red de casa, con un solo usuario, y un
login más delante de un formulario que se abre medio dormido a las siete de la
mañana costaba más de lo que protegía. Antes había un `basic_auth` en Caddy con
un hash de bcrypt; se quitó.

**Lo que eso deja abierto**, escrito aquí para que nadie tenga que deducirlo:
cualquier cacharro conectado al wifi puede

- pedir `/api/export`, que es el histórico entero —sueño, HRV, RPE y el registro
  de la lumbar—, y
- llamar a `POST /api/checkin`, que no solo lee: **crea un check-in y dispara
  una decisión**.

`DRY_RUN=true` hoy corta la escritura en Hevy y en Telegram, pero **no cuenta
como protección**: el propósito de esta fase es precisamente llegar a quitarlo,
y entonces el riesgo empeora justo el día en que uno ha dejado de pensar en él.

**Esta decisión NO viaja al despliegue definitivo.** En Umbrel va el `app_proxy`
delante, que pone el login de Umbrel, y eso se queda como está. El trato es «sin
contraseña en la LAN del PC», no «sin contraseña».

Para que no se cuele en otro sitio por descuido, **la aplicación lo canta en el
log en cada arranque** —por log y no por Telegram: Telegram es el canal de la
decisión de la mañana y un aviso de despliegue ahí, o se ignora, o convierte el
mensaje útil en ruido—. Lo gobierna `AUTH_FRONT`:

| `AUTH_FRONT` | qué hace el arranque |
|---|---|
| sin poner | **WARNING**: sirviendo sin autenticación conocida, nadie ha declarado qué hay delante |
| `ninguna` | **WARNING**: sirviendo sin autenticación, y está declarado así a propósito |
| `proxy` | INFO: hay un proxy con credenciales delante; sin aviso |

El defecto es el ruidoso a propósito. Desde dentro del contenedor no hay forma
de distinguir «detrás del `app_proxy`» de «publicado en crudo», así que no se
puede comprobar: se declara. Y si nadie declara nada, se avisa —lo contrario
haría que el único caso peligroso fuese justo el silencioso—. `AUTH_FRONT=proxy`
es lo único que quita el aviso, y ponerlo cuando no hay proxy es mentirse a uno
mismo por escrito.

El compose de pruebas ya trae `AUTH_FRONT=ninguna`, que es lo que hace que el
aviso diga «a propósito» en vez de «nadie ha declarado nada».

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml up -d --build
```

**El `--build` no es opcional.** El compose de la raíz trae `build:` *y*
`image: adaptive-training:0.1.0`; con `up -d` a secas, si esa etiqueta ya existe,
Docker levanta la imagen vieja **sin decir nada**. Se ve como un formulario que
no tiene los avisos de salud, o un `/api/health` sin `clock`. Pasó aquí.

Desde el móvil: `http://<ip-del-pc>:8317` (la IP, con `ipconfig`). Entra directo.
Si el navegador del móvil se queda colgando —y desde el PC sí responde—, es el
cortafuegos de Windows: hay que dejar pasar el puerto **para la red privada**, no
para la pública.

**Sigue habiendo un proxy aunque ya no pida nada**, por dos razones que
sobreviven a la contraseña. Lo que sale a la red es solo el 8317; el 8000 de la
aplicación sigue atado a `127.0.0.1`, para los `curl` desde el propio PC. Y un
proxy delante es la **misma forma** que tendrá el despliegue de verdad, así que
esta fase ensaya aquella en vez de inventarse otra.

**En Windows, `data/` deja de verse desde el explorador.** El bind mount de la
raíz no vale aquí: SQLite abre la base en modo WAL y el bind mount de Docker
Desktop no soporta ese bloqueo —el contenedor muere al arrancar con
`sqlite3.OperationalError: disk I/O error` y entra en bucle de reinicio—. El
override usa un volumen nombrado. Es un problema **de Windows y solo de
Windows**: en Umbrel el bind mount funciona, así que el apaño no viaja. Lo que
hay que poder sacar de ahí son las copias previas a cada escritura en Hevy, que
son lo único que permite deshacerla:

```bash
docker cp adaptive-training:/app/data/hevy_backups ./hevy_backups
docker cp adaptive-training:/app/data/app.db ./app.db
```

**El PC tiene que estar despierto a las 06:30, 09:00 y 22:30.** Si duerme, esos
trabajos no se ejecutan y no hay aviso posible: un portátil suspendido se lee
desde el móvil exactamente igual que un día de descanso. Es la razón principal
por la que esta fase es temporal y el destino es una máquina encendida.

## Dónde vive cada cosa

```
config.yaml     Las reglas. Se versiona en git. Se edita sin reconstruir nada.
.env            Los secretos. Ni a git ni a la imagen de Docker.
data/           Todo lo que tiene que sobrevivir a una actualización:
  app.db          base de datos (check-ins, decisiones, estado del motor)
  garmin_tokens/  sesión de Garmin, para no volver a hacer login (y comerse 429)
  cache/          actividades descargadas
  hevy_backups/   copia de cada rutina ANTES de escribirla
```

`data/hevy_backups/` es lo único que permite deshacer una escritura en Hevy. Si
ese directorio no persiste, un error deja de tener vuelta atrás. Es el motivo de
que todo cuelgue de un único volumen.

## Actualizar

A mano:

```bash
git pull
docker compose up -d --build
```

Con el proxy temporal de las pruebas hay que repetir los dos `-f` **siempre**:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml up -d --build
```

Un `docker compose up -d` a secas, por costumbre, recrea la aplicación con el
bind mount de la raíz y en Windows la deja en bucle de reinicio
(`sqlite3.OperationalError: disk I/O error`). El proxy sigue en pie —compose
solo avisa de que lo ve huérfano—, así que desde el móvil no se ve un error de
conexión sino un **502 del proxy**. Los datos no se pierden: siguen en el volumen
nombrado, sin montar. Se arregla repitiendo el comando de arriba, con los dos
`-f`. Comprobado.

En Umbrel se publica una imagen nueva y se actualiza desde su interfaz;
el procedimiento está en [`umbrel/README.md`](umbrel/README.md).

La base de datos se pone al día sola cuando los cambios son seguros -añadir una
columna que admite nulos, rehacer una tabla vacía-. Cuando no lo son **se niega
a arrancar y dice exactamente qué migrar a mano**. Eso es a propósito: seguir
significaría reventar más tarde, de noche y sin nadie delante.

## Cuando algo va mal

```bash
docker compose logs -f --tail=100
curl -s localhost:8000/api/health | python3 -m json.tool
curl -s "localhost:8000/api/decision?day=$(date +%F)" | python3 -m json.tool
```

En Umbrel no hay `localhost:8000` -el contenedor no publica puertos-, así que se
pregunta desde dentro:

```bash
docker logs -f --tail=100 roolez-adaptive-training_server_1
docker exec roolez-adaptive-training_server_1 \
  python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health')))"
```

Un trabajo que falla o que no llega a ejecutarse **manda un aviso por
Telegram**. Si el sistema calla, es que no ha fallado.

Sacar los datos, que son del usuario:

```bash
curl -s "localhost:8000/api/export?desde=2026-01-01&hasta=$(date +%F)" > entrenos.csv
```

## Desarrollo

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python -m app.cli --dry-run
```

**Los iconos PNG no se editan.** Se dibujan desde los dos SVG de
`static/icons/` con `python scripts/generar_iconos.py`; si tocas un SVG, vuelve
a pasarlo. La suite compara píxeles y avisa si se te olvida, porque un PNG
guardado no tiene otra forma de dejar de ser verdad: se cambia un color en el
SVG y quedan dos semáforos distintos según por dónde se abra la aplicación.

## Cómo está montado

```
app/engine/      El motor: señales, reglas, frenos, progresión, decisión.
app/integrations/  Garmin (lectura), Hevy (lectura y escritura), Telegram.
app/api.py       FastAPI: la API del formulario y, en su `lifespan`, los
                 trabajos del día. Un solo proceso: dos planificadores sobre la
                 misma base son dos decisiones pisándose el mismo día.
app/scheduler.py APScheduler con las horas del `config.yaml`.
static/          La PWA. Sin dependencias ni compilación.
tests/           782 tests.
```

### La idea que se repite en todo el código

Un sistema que decide solo y se equivoca en voz alta se arregla. Uno que se
equivoca en silencio, no: no llega mensaje, y no llegar mensaje se parece
demasiado a un día de descanso. Por eso hay tantos sitios donde esto se **niega
a continuar** en vez de apañárselas —una base desfasada, un `config.yaml` que
promete algo que el código no hace, un día del calendario que no declara qué
toca—. Cada uno de ellos era antes un fallo mudo.
