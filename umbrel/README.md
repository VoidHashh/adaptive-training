# Instalación en Umbrel

Umbrel no instala esto con `docker compose up`. Va por su marco de
aplicaciones, y la razón es de seguridad:

**La aplicación no tiene autenticación propia.** `/api/state`, `/api/decision` y
`/api/export` —el CSV con el histórico entero— contestan a quien pregunte.
Instalada a mano y publicada en la red local, eso deja el historial de
entrenamiento y de salud a la vista de cualquiera que esté en el wifi. Instalada
como aplicación de Umbrel, el `app_proxy` pone delante el login de Umbrel sin
que haya que escribir una línea de código.

Por eso el `docker-compose.yml` de este directorio **no publica puertos**: al
contenedor se llega solo por el proxy.

---

## 1. Publicar la imagen

Umbrel instala, no compila: rechaza `build:` a propósito. Hace falta una imagen
ya construida en un registro.

```bash
docker login ghcr.io
docker buildx build --platform linux/amd64,linux/arm64 \
  --build-arg BUILD_SHA="$(git rev-parse --short HEAD)" \
  --build-arg BUILD_DATE="$(date -Iseconds)" \
  -t ghcr.io/<usuario>/adaptive-training:0.1.0 --push .
docker buildx imagetools inspect ghcr.io/<usuario>/adaptive-training:0.1.0
```

Los dos `--build-arg` no son opcionales en la práctica, aunque el `Dockerfile`
los deje vacíos sin protestar. La etiqueta `0.1.0` **no se mueve entre
versiones**: es el mismo texto desde hace decenas de cambios, así que sin esta
marca «¿está corriendo el arreglo de ayer?» no se puede contestar mirando el
sistema, y esa pregunta se hace después de cada arreglo. Con ella, `/api/health`
devuelve el `build` y se contesta desde el móvil.

Las dos arquitecturas están comprobadas: la imagen construye y arranca en
`linux/arm64` (Raspberry, Umbrel Home) y en `linux/amd64`. `uvloop` y
`httptools` compilan en las dos —es lo que hace el `build-essential` de la
primera etapa del `Dockerfile`—.

Del `imagetools inspect` se copia el digest **del índice** (el de arriba, el
multiarquitectura), no el de una arquitectura suelta, y se fija en el
`docker-compose.yml`:

```yaml
image: ghcr.io/<usuario>/adaptive-training:0.1.0@sha256:<digest-del-índice>
```

> Si el repositorio es privado hay que hacer `docker login ghcr.io` **en el
> Umbrel** antes de instalar, o la instalación falla al descargar.

## 2. La tienda privada

Se parte de un fork de `getumbrel/umbrel-community-app-store`. En la raíz:

```yaml
# umbrel-app-store.yml
id: "roolez"
name: "Roolez"
```

Y un directorio por aplicación:

```
roolez-adaptive-training/
  umbrel-app.yml
  docker-compose.yml
```

**El `id` de la aplicación tiene que empezar por el `id` de la tienda.** Umbrel
lo comprueba en código; una aplicación que no cumpla no da error, simplemente
**no aparece** en la lista. Si la tienda se llama de otra forma, hay que cambiar
a la vez el `id:` del `umbrel-app.yml` y el `APP_HOST` del compose, que lo
incluye:

```yaml
APP_HOST: <id-de-la-aplicación>_server_1
```

Luego, en la interfaz de Umbrel: **App Store → … → Community App Stores**, y se
pega la URL del fork.

## 3. Los ficheros que van en `${APP_DATA_DIR}`

`${APP_DATA_DIR}` es `~/umbrel/app-data/<id-de-la-aplicación>/`. Hay que dejar
dos cosas **antes de abrir la aplicación**:

```bash
ssh umbrel@<host>
APP=~/umbrel/app-data/roolez-adaptive-training
mkdir -p "$APP/data"

# Las reglas. Sin esto, Docker crea un DIRECTORIO llamado config.yaml,
# `load_config` no encuentra ningún YAML y el contenedor no levanta.
curl -fsSL https://raw.githubusercontent.com/<usuario>/adaptive-training/master/config.yaml \
  -o "$APP/config.yaml"

# Los secretos.
nano "$APP/.env"
chown -R 1000:1000 "$APP"
```

El `.env` es el mismo de siempre; el modelo está en `.env.example`, en la raíz
del repositorio:

| Variable | De dónde sale |
|----------|---------------|
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | La cuenta de siempre. |
| `HEVY_API_KEY` | Hevy → Ajustes → Developer (necesita Hevy Pro). |
| `TELEGRAM_BOT_TOKEN` | Lo da `@BotFather` al crear el bot. |
| `TELEGRAM_CHAT_ID` | Escribirle al bot y mirar `api.telegram.org/bot<TOKEN>/getUpdates`. |

**`DRY_RUN=true` mientras dure la comparación.** El sistema decide, guarda y
registra, pero no toca Hevy ni manda Telegram. La aplicación lo dice en la
pantalla del móvil, en ámbar, para que un día sin mensaje no se confunda con una
avería.

### Si falta el `.env`

La aplicación **arranca igual** y lo primero que enseña en el móvil es qué
claves faltan. Es deliberado: `env_file` va con `required: false` porque un
`compose up` que falla se ve en la interfaz de Umbrel como un error genérico de
instalación, sin ningún sitio donde leer la causa.

Ese apaño solo es aceptable **porque se avisa**. El aviso lo pinta la PWA
leyendo `secrets_missing` de `/api/health`. Si algún día se quita, hay que
volver a poner `env_file` en `required`, o una instalación a medias vuelve a ser
un formulario que se envía y no hace nada.

## 4. Comprobar que ha arrancado entero

```bash
ssh umbrel@<host>
docker ps --filter name=roolez-adaptive-training
docker exec roolez-adaptive-training_server_1 \
  python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health')))"
```

Mirar tres cosas, no solo el `status`:

- **`secrets_missing` vacío.**
- **`scheduler.running` en `true` y los tres trabajos con hora.** Sin
  planificador la aplicación sirve el formulario, contesta `ok` y no decide
  nunca. Desde fuera se parece muchísimo a una semana de descanso.
- **`clock.matches` en `true`.** Compara el reloj del proceso con la zona del
  `config.yaml`. Si el `TZ` del compose no ha entrado, un check-in enviado entre
  las 00:00 y las 02:00 se guarda con la fecha de ayer, y por la mañana se
  decide como si no lo hubiera habido.

  Mirar el `+02:00` de las horas de los trabajos **no vale**: se construyen con
  la zona del `config.yaml` y salen en `+02:00` aunque el contenedor esté en
  UTC. Comprobado levantando la imagen sin `TZ`, con `TZ=Europe/Madrid` y con
  `TZ=Asia/Tokyo`: las tres dan la misma hora en los trabajos y solo `clock`
  distingue.

Las tres las pinta también la PWA al abrirla, que es donde se van a mirar de
verdad.

## 5. El formulario en el móvil

`http://umbrel.local:8317` (o el puerto que quede en el `umbrel-app.yml`).
Pedirá el login de Umbrel: eso es el `app_proxy` haciendo su trabajo.

**No se instalará como PWA, y no es un fallo de la aplicación.** "Añadir a
pantalla de inicio" deja un acceso directo, pero el service worker **no se
registra sobre HTTP**: los navegadores solo lo permiten en contexto seguro
(HTTPS, o `localhost`), y Umbrel sirve las aplicaciones en HTTP dentro de la red
local. Consecuencias concretas:

- No hay arranque sin conexión. Sin red no se abre nada, en vez de abrirse y
  decir que no hay red.
- Todo lo demás —el formulario, el envío, los avisos— funciona igual.

Nada de esto rompe el uso normal, que es abrirlo por la mañana con wifi. Si
alguna vez se quiere el modo aplicación de verdad, hace falta HTTPS por delante
(un túnel, o un proxy con certificado); merece la pena mirar antes si el
`requiresHttps: true` del manifiesto de Umbrel cubre este caso, porque de ser
así es una línea.

## 6. Actualizar

Se publica una imagen nueva con su etiqueta, se cambia `version:` en el
`umbrel-app.yml` y `image:` en el compose, y se actualiza desde la interfaz de
Umbrel.

La base de datos se pone al día sola cuando los cambios son seguros. Cuando no
lo son **se niega a arrancar y dice exactamente qué migrar a mano**: seguir
significaría reventar más tarde, de noche y sin nadie delante.

`${APP_DATA_DIR}` sobrevive a las actualizaciones, y ahí está `data/`, que
incluye `hevy_backups/` —lo único que permite deshacer una escritura en Hevy—.
