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

**Esto ya no se hace a mano.** `.github/workflows/docker.yml` construye y
publica la imagen en cada empujón a `main`, con el `GITHUB_TOKEN` que Actions
fabrica para esa ejecución: no hay ningún `docker login ghcr.io` que hacer en el
portátil, ni un token de paquetes que guardar y rotar.

Aquí ponía el `docker buildx ... --push` desde el PC, y se ha quitado por algo
más que comodidad: construyendo en local, la imagen sale del DISCO de quien la
construye, no del commit. Un fichero a medio editar entra en la imagen sin estar
en el repositorio, y entonces el `BUILD_SHA` que enseña `/api/health` miente:
señala un commit que no describe lo que está corriendo. En el taller el contexto
es el árbol del commit y no puede ser otra cosa.

Se publican tres etiquetas y cada una contesta una pregunta distinta:

| Etiqueta | Para qué |
|----------|----------|
| `:0.1.<N>` (la de `pyproject.toml`, que el propio taller sube en cada empujón) | La que instala Umbrel. Cada empujón publica una nueva y la anterior se queda, así que siempre se puede volver. |
| `:latest` | Un `docker run` rápido sin mirar versiones. |
| `:<sha>` | La única que no se mueve nunca. A la que se vuelve para reinstalar exactamente lo de antes de un cambio. |

Para ver si el taller ha terminado y con qué:
<https://github.com/VoidHashh/adaptive-training/actions>

**El paquete se descarga sin credenciales, comprobado el 25/09/2026.** Aquí
ponía que GitHub crea el paquete en privado aunque el repositorio sea público y
que había que cambiarlo a mano; no es verdad en este caso —el paquete heredó la
visibilidad del repositorio—, y un aviso falso en un documento de instalación
manda a buscar un problema que no existe. La forma de saberlo sin creerse a
nadie es pedir el manifiesto sin token, que es justo lo que hará el Umbrel:

```bash
IMG=voidhashh/adaptive-training
T=$(curl -s "https://ghcr.io/token?scope=repository:$IMG:pull&service=ghcr.io" | jq -r .token)
curl -s -o /dev/null -w '%{http_code}
' -H "Authorization: Bearer $T"   https://ghcr.io/v2/$IMG/manifests/0.1.0      # 200 = publico; 401 = privado
```

Si algún día diera 401, se cambia en
<https://github.com/users/VoidHashh/packages/container/adaptive-training/settings>
→ *Change visibility* → **Public**. Sin eso, la instalación falla al hacer
`pull` y en la interfaz de Umbrel es un error genérico sin causa legible.

> Solo `linux/amd64`. El Umbrel de destino es x86_64; añadir `linux/arm64`
> significa compilar `uvloop` y `httptools` bajo emulación QEMU —los dos
> paquetes sin rueda para ARM que justifican el `build-essential` del
> `Dockerfile`— y eso tarda más que todo lo demás junto. El día que haya un
> Umbrel de Raspberry delante, se añade la plataforma al taller.

## 2. La tienda

La aplicación vive en la tienda que ya está dada de alta en este Umbrel:
**<https://github.com/VoidHashh/PlanB>**, cuyo `umbrel-app-store.yml` declara
`id: planb`. No hay nada que añadir en la interfaz de Umbrel.

**El `id` de la aplicación tiene que empezar por el `id` de la tienda.** Umbrel
lo comprueba en código (`.filter(app => app.id.startsWith(meta.id))`) y una
aplicación que no cumpla **no da error: simplemente no aparece en la lista**. De
ahí `planb-adaptive-training`, un prefijo que no describe la aplicación sino la
tienda que la sirve.

En ese repositorio hay que dejar una carpeta con tres ficheros, que es
exactamente lo que ya tienen `planb-panel` y `planb-bitstatus`:

```
planb-adaptive-training/
  umbrel-app.yml       <- copia de umbrel/umbrel-app.yml de este repositorio
  docker-compose.yml   <- copia de umbrel/docker-compose.yml de este repositorio
  icon.svg             <- lienzo CUADRADO (Umbrel pinta tarjetas cuadradas)
```

Los dos YAML son **copias**: el original vive aquí, donde
`tests/test_despliegue.py` los ata al `Dockerfile` y a `pyproject.toml`; la
copia vive allí, donde Umbrel los lee. **La copia la hace sola un taller que
vive dentro de `VoidHashh/PlanB`** (`.github/workflows/sincronizar-adaptive-training.yml`):
se trae los dos ficheros del `main` de este repositorio y los commitea si han
cambiado. Vive de aquel lado a propósito: así su propio `GITHUB_TOKEN` basta y
no hay ninguna credencial que crear ni rotar.

Lo que hay que saber de ese taller es su reloj. Está declarado «cada diez
minutos», y **GitHub no lo cumple**: en un repositorio con poca actividad lo
retrasa horas —la primera noche corrió tres veces en ocho horas—. Si hace falta
ya, se lanza a mano en PlanB → *Actions* → *Sincronizar adaptive-training* →
*Run workflow*.

El `icon.png` va en la tienda y no aquí: el campo `icon:` del manifiesto es una
URL completa a `raw.githubusercontent.com`. En la tienda oficial Umbrel reescribe
esos nombres contra su propio repositorio de imágenes; en una privada no hay tal
reescritura, y un nombre suelto deja la aplicación con el hueco gris.

## 3. Los ficheros que van en `${APP_DATA_DIR}`, ANTES de instalar

**La primera instalación falla si no se hace esto, y falla con un error que no
dice por qué.** Pasó el 25/09/2026, en la interfaz de Umbrel:

```
Command failed with exit code 1:
/opt/umbreld/source/modules/apps/legacy-compat/app-script install planb-adaptive-training
```

Eso es todo lo que se ve. Lo que había pasado por debajo:

1. Umbreld creó `${APP_DATA_DIR}` y lanzó `docker compose up`.
2. El compose monta `${APP_DATA_DIR}/config.yaml` y ese fichero no existía.
   **Docker, ante un origen de bind mount que no existe, crea un DIRECTORIO.**
3. `load_config` se encontró un directorio donde esperaba un YAML, el contenedor
   murió, y umbreld deshizo la instalación —la aplicación ni siquiera aparece en
   la lista de instaladas—, pero **dejó `${APP_DATA_DIR}` en pie**, con el
   directorio `config.yaml` dentro para que la siguiente vez vuelva a fallar.

Y aquí estaba el gazapo de este documento: decía «antes de **abrir** la
aplicación». Era falso. Hay que hacerlo antes de **instalarla**, porque la
instalación no llega a abrirse: se cae antes.

### El orden que funciona

`${APP_DATA_DIR}` es `~/umbrel/app-data/<id-de-la-aplicación>/`, y no existe
hasta que se instala. La salida es crearlo a mano antes: si ya está, umbreld lo
respeta y no lo toca.

```bash
ssh umbrel@<host>
APP=~/umbrel/app-data/planb-adaptive-training
mkdir -p "$APP/data"

# Las reglas. Un FICHERO, no un directorio (ver arriba).
curl -fsSL https://raw.githubusercontent.com/VoidHashh/adaptive-training/main/config.yaml   -o "$APP/config.yaml"

# Los secretos. Opcional: sin esto la aplicación arranca y dice qué le falta.
nano "$APP/.env"
```

**Sin `sudo`, y no es un detalle de comodidad.** El usuario `umbrel` es uid
1000, que es el mismo con el que corre el contenedor (`user: "1000:1000"`), así
que todo lo que cree él ya tiene el dueño correcto. Aquí ponía un `chown -R
1000:1000` que además de pedir contraseña tapaba el problema de verdad: **si a
`data/` lo crea Docker en vez de tú, sale `root:root` y el contenedor no puede
escribir su propia base de datos.** Arranca, responde, y no persiste nada.

### Si la instalación ya ha fallado

No hace falta root ni reinstalar Umbrel. El directorio de la aplicación es del
usuario `umbrel`, así que se puede vaciar aunque lo de dentro sea de `root`:

```bash
APP=~/umbrel/app-data/planb-adaptive-training
rmdir "$APP/config.yaml"        # el directorio que creó Docker
rmdir "$APP/data" && mkdir "$APP/data"   # para que salga con uid 1000
curl -fsSL https://raw.githubusercontent.com/VoidHashh/adaptive-training/main/config.yaml   -o "$APP/config.yaml"
```

Y volver a darle a **Install** en la interfaz. **No a *Uninstall* primero**: eso
sí se lleva `${APP_DATA_DIR}` por delante, y con él la base de datos y las copias
de Hevy.

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

### Si esta instancia NO es la que manda

Mientras el sistema de verdad siga corriendo en otra máquina —el PC, por
ejemplo—, el `.env` de aquí lleva además estas dos:

```bash
SCHEDULER_ENABLED=false
DRY_RUN=true
```

No es prudencia de más. `scheduler_enabled` vale `true` por defecto, así que un
`.env` copiado tal cual arranca los tres trabajos del día **también aquí**, y
entonces hay dos planificadores decidiendo el mismo día y dos procesos
reescribiendo la misma rutina en Hevy. El comentario de `app/settings.py` ya
nombra este caso: *«dos planificadores sobre la misma base son dos decisiones
pisándose el mismo día»*. `DRY_RUN=true` es el segundo cinturón: aunque algo
dispare una decisión, no sale de aquí.

Con las dos puestas, la aplicación se puede abrir, enseñar el formulario y
servir `/api/health` sin tocar nada de fuera. Es lo que hace falta para
comprobar que la instalación está bien.

**Para que tome el relevo: apagar primero el otro**, y solo después poner
`SCHEDULER_ENABLED=true` y `DRY_RUN=false` aquí. En ese orden, porque el
solapamiento es lo caro.

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
docker ps --filter name=planb-adaptive-training
docker exec planb-adaptive-training_server_1 \
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

**Un `git push` y nada más.** Así es desde el 26/09/2026, y antes no lo era:
el 25 el Umbrel pasó horas sirviendo `f9a5a4a` con dos commits de código por
delante, porque la etiqueta de la imagen no se movía y Umbrel no veía ninguna
actualización que ofrecer. Desde fuera se parecía a la aplicación funcionando,
que es exactamente el modo de fallo que este proyecto persigue.

Lo que pasa después del `push`, y quién lo hace:

1. **El taller de este repositorio** fija la versión `0.1.<nº de commits>` en
   los cuatro sitios con `scripts/fijar_version.py`, la devuelve al repositorio
   en un commit `[skip ci]`, y publica la imagen con esa etiqueta.
2. **El taller de la tienda** (sección 2) se trae los dos YAML cuando GitHub
   ejecuta su cron. Horas, no minutos.
3. **Umbrel ofrece la actualización** al ver una versión mayor que la
   instalada. Ese clic es el único paso humano que queda.

Probado de punta a punta la primera vez: `push` `93bea18` → versión `0.1.193`
→ tienda `a8e6ed1` → Umbrel actualizado y decidiendo el día siguiente.

**Lo que empeora, y muerde:** después de cada `push` la rama local queda una
commit por detrás —la de la versión—. El siguiente `push` se rechaza hasta un
`git pull --rebase`.

**Si alguna vez hay que mover la versión a mano**: `python
scripts/fijar_version.py 0.1.200`. Nunca fichero por fichero: el script afirma
cuántas veces tiene que casar cada patrón y vuelve a leer el disco para
comprobar que quedó escrito, que es justo lo que un `sed` no hace.

Y para contestar «¿está corriendo lo de hoy?» sin fiarse de nada de lo anterior,
`/api/health` devuelve el `build`, que es el SHA del commit que construyó la
imagen. Esa es la respuesta buena, y se lee desde el móvil.

La base de datos se pone al día sola cuando los cambios son seguros. Cuando no
lo son **se niega a arrancar y dice exactamente qué migrar a mano**: seguir
significaría reventar más tarde, de noche y sin nadie delante.

`${APP_DATA_DIR}` sobrevive a las actualizaciones, y ahí está `data/`, que
incluye `hevy_backups/` —lo único que permite deshacer una escritura en Hevy—.
