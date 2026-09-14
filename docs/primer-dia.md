# El primer día

Esto no es el manual de instalación —ése está en el [README](../README.md#instalación)
y sirve para montar el sistema desde cero en una máquina vacía—. Esto es el
guión del **lunes 14 de septiembre de 2026**, que es el día que `program.start`
señala y el primero en el que el sistema decide algo que va a gobernar un
entrenamiento de verdad.

La diferencia importa. Instalar es un problema resuelto; lo que no está resuelto
es **qué mira uno el primer día para saber si lo que ha arrancado está vivo o
solo parece vivo**, que no es lo mismo y desde fuera se ven igual.

**Se arranca con los dos frenos abiertos.** No hay fase en seco: `DRY_RUN=false`
y `integrations.hevy.write_enabled: true`. El lunes por la mañana llega un
Telegram de verdad y la rutina de Hevy se reescribe de verdad.

---

## 0. El comando, y por qué lleva dos `-f`

Todo lo que hay debajo usa este prefijo. Copiarlo mal no da error: da otra
instalación.

```bash
cd D:/hevy2garmin-test
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml <lo que sea>
```

`docker compose` **a secas también funciona**, y ése es el problema. El compose
de la raíz está escrito para el Umbrel: monta `./data` como *bind mount*. La
superposición `pruebas-lan` lo sustituye por el volumen nombrado
`hevy2garmin-test_datos-pruebas`, precisamente porque en Windows ese bind mount
da `disk I/O error` con SQLite en modo WAL. Además añade el proxy del 8317 y
`AUTH_FRONT=ninguna`.

Sin los dos `-f`, el contenedor arranca apuntando a **otro directorio de datos**:
otra base, otras copias de seguridad de Hevy, otros tokens de Garmin. Y contesta
`status: ok` con la misma convicción.

> Pasó el sábado 12. El contenedor llevó el día entero así. Se descubrió al ir a
> escribir esta guía y mirar `docker inspect`, no porque nada fallara.

**Cómo se ve desde fuera, sin `docker inspect`:**

```bash
curl -s localhost:8317/api/health | python -m json.tool
```

En este PC, `"auth_front"` tiene que decir **`"ninguna"`**. Si dice
`"sin_declarar"`, el contenedor se levantó con el compose equivocado y está
usando un directorio de datos que no es el de esta guía.

### La comprobación de después de CUALQUIER cambio

Vale para `config.yaml`, para `.env`, para el `Caddyfile` y para el código.
Ninguno de los cuatro se relee en caliente.

```bash
# 1. recrear (añade `build app` antes si has tocado código Python)
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  up -d --force-recreate app

# 2. verificar que el cambio está DENTRO, no solo en el disco
curl -s localhost:8317/api/health | python -m json.tool
```

Lo que hay que leer en esa respuesta, y qué significa cada cosa:

| Campo | Tiene que decir | Si no |
|---|---|---|
| `config_file.in_sync` | `true` | el YAML del disco **no** es el que está decidiendo |
| `config_file.file_hash` | igual a `loaded_hash` | lo mismo, con los dos números delante |
| `auth_front` | `"ninguna"` | levantado con el compose equivocado (ver arriba) |
| `dry_run` | `false` | el `.env` no ha entrado: Telegram no enviará |
| `writes.hevy_write_enabled` | `true` | el config no ha entrado: Hevy no se escribirá |
| `writes.telegram_send_enabled` | `true` | escribiría en Hevy sin avisar por Telegram → §2.3 |
| `writes.pending_write` | `null` | hay una rutina en estado desconocido → §3 |
| `writes.pending_error` | `null` | no se ha podido **mirar** si la hay, que no es lo mismo |
| `writes.stale_write` | `null` | en Hevy hay ahora mismo una sesión que hoy **no** toca → abajo |
| `writes.stale_error` | `null` | no se ha podido mirar si la hay |
| `secrets_missing` | `[]` | arranca igual y luego no hace su trabajo |
| `scheduler.running` | `true` con **4** trabajos | sirve la PWA, contesta `ok` y no decide nunca |
| `clock.matches` | `true` | un check-in de madrugada se guarda con la fecha de ayer |

`config_file` se calcula releyendo el YAML del disco **en cada petición** y
comparándolo con el que hay cargado en memoria. Es el único campo que no se
puede quedar obsoleto, y existe por la avería que este proyecto ha cometido ya
cuatro veces: *el valor que se lee no es el valor que se usa* (imagen vieja,
esquema viejo, Caddyfile viejo, compose equivocado).

Y un detalle operativo que cuesta encontrar: **el `Caddyfile` no se puede
recargar en caliente**. `caddy reload` falla con `connection refused` porque el
propio fichero pone `admin off`, que apaga la API de administración a propósito.
La única forma de aplicar un cambio es recrear el contenedor del proxy.

---

## 1. Estado en el que te lo dejo (sábado 12)

Comprobado, no supuesto:

| Cosa | Valor |
|---|---|
| Imagen | `adaptive-training:0.1.0`, reconstruida el 12 |
| Datos | volumen `hevy2garmin-test_datos-pruebas` |
| `config_hash` | `8a0e1e304324a6be`, `in_sync: true` |
| `auth_front` | `ninguna` |
| `DRY_RUN` | **`false`** |
| `integrations.hevy.write_enabled` | **`true`** |
| `integrations.telegram.send_enabled` | `true` (el tercer interruptor, §2.3) |
| `daily_metrics` | 180 filas, 2026-03-15 → 2026-09-10 |
| `activities` | 58 |
| `decisions` / `checkins` / `notifications` | 0 / 0 / 0 — nada decidido todavía |
| `hevy_writes` | 0 — ninguna rutina escrita por el sistema |
| Copias de Hevy | 3, todas de `dia_3`, de la prueba de reversión |
| `garmin_tokens/` | intacto; la sesión se reanuda sin volver a hacer login |
| Puerto 8317 | 200, **sin contraseña**, a propósito |
| Planificador | 4 trabajos: `garmin_fetch`, `decision_fallback`, `reconcile`, `perception_notice` |

**Cuatro y no cinco, y no falta ninguno.** Se registran cinco: el quinto es
`backfill_wellness`, que no tiene hora sino un retraso de minuto y medio desde el
arranque —recupera los días de bienestar que falten y se acabó—. A los dos
minutos de recrear el contenedor ya se ha ejecutado y desaparece de la lista.
Verlo ahí significa que acabas de arrancar; no verlo es lo normal.

**La rutina `dia_3` de tu Hevy se usó como banco de pruebas el sábado.** Se
destruyó a propósito (de 11 ejercicios a 2) y se revirtió. Está como estaba: 11
ejercicios, título «Día 3», idéntica campo a campo a la copia previa. Se eligió
`dia_3` porque la variante de calendario activa (`with_pool`) no la programa
nunca, así que el `dia_1` del lunes no podía ser daño colateral.

---

## 2. Lo que el sistema hace hacia fuera, con los dos frenos abiertos

La lista completa. Nada de esto es hipotético: sale de leer el código, no de
recordarlo.

### 2.1 Escribe en Hevy

**Una sola cosa, un solo sitio:** `PUT /v1/routines/{id}` sobre la rutina del
día. Sólo los días que la tienen (lunes y jueves con el calendario `with_pool`;
sábado y domingo son bici y no se toca nada).

`PUT` es **reemplazo total**. No añade: sustituye la rutina entera por lo que
lleva el cuerpo. Por eso:

- Antes de cada PUT se guarda una copia de la rutina tal como está, en
  `data/hevy_backups/<routine_id>/<fecha>-<hora>.json`. **Sin copia verificada
  no se escribe**, y eso está en el código, no en el config.
- Justo antes del PUT se escribe una *marca de escritura en curso*, y se borra
  justo después. Si el proceso muere entre las dos cosas, la marca sobrevive y
  `/api/health` lo dice en `writes.pending_write`.
- El cuerpo que viaja lo construye `cuerpo_para_put`, que es una lista blanca de
  campos. Un campo que Hevy no reconozca es **error duro** y no un descarte
  callado: se aborta el PUT, la copia queda hecha, y el Telegram lo dice.

Se escribe **una vez por decisión, no una vez por día**. Volver a enviar el
formulario del check-in decide otra vez y escribe otra vez (con su copia nueva).
No hay nada que lo impida, y normalmente no importa: se escribe lo mismo. Importa
si las respuestas cambian, porque entonces la rutina cambia también.

**Y si la decisión nueva no escribe, se deshace la vieja.** La secuencia es
ésta: a las 09:00 no ha llegado el check-in, el trabajo de respaldo decide con
Garmin, sale verde y escribe el `Día 1`; a las 10:30 llega el check-in, sale
rojo, y la sesión de hoy es recuperación, que no toca Hevy. Antes eso se
saltaba -«hoy la sesión no toca Hevy»- y el día terminaba con un Telegram
diciendo *Recuperación* y la app con el `Día 1` entero puesto. Ahora la rutina
se devuelve a como estaba **antes de la primera escritura del día**, y el día
queda idéntico a como habría quedado si el respaldo no hubiera corrido.

Las dos cosas se anotan en `hevy_writes`, así que un día puede tener varias
filas y **el orden es el dato**. Se ven con `app.rutina ver` (§ más abajo):

| `status` | Qué pasó |
|---|---|
| `reverted` | se deshizo lo que se había escrito antes hoy |
| `stale` | había que deshacerlo y **no se pudo** |

`stale` es el único que deja trabajo pendiente para una persona, y por eso sale
por tres sitios: la primera línea del Telegram, `writes.stale_write` en
`/api/health`, y un aviso rojo en la pantalla del móvil. El texto es el mismo en
los tres y está redactado para poder actuar, no sólo para enterarse: *«en Hevy
ha quedado el Día 1 de una decisión anulada; abre Hevy y NO lo hagas: hoy toca
Recuperación»*. El aviso se apaga solo al día siguiente, cuando el trabajo de
las 09:00 vuelve a escribir.

### 2.2 Lee de Hevy

`GET /v1/routines/{id}` antes de cada escritura, y `GET /v1/workouts` en la
reconciliación de las 22:30 y en `POST /api/reconcile`. Leer no depende de
ningún interruptor: se hace siempre.

### 2.3 Manda Telegram

Al chat de `TELEGRAM_CHAT_ID`. **Cuatro cosas distintas**, y conviene saber que
son cuatro porque llegan por el mismo sitio:

1. **El mensaje de la mañana.** Uno por decisión. Con `DRY_RUN=false` se envía
   de verdad. Si vuelves a enviar el formulario, llega otro.
2. **El aviso de disociación**, a las 09:30. Cuenta los desajustes de ayer entre
   lo percibido y lo medido. Sólo cuando hay algo que contar.
3. **Los fallos del planificador.** Cualquier trabajo que reviente o que se
   pierda manda un aviso. **Esto NO mira `DRY_RUN`**: se envía siempre, también
   en seco. Es deliberado —el modo de fallo de este sistema es el silencio, y un
   aviso de avería que también se calla en seco no avisa de nada— pero es el
   único efecto hacia fuera que no tiene freno, y merece estar escrito.
4. **El error de una escritura de Hevy** va dentro del mensaje de la mañana, en
   la cabecera, no como mensaje aparte.

**Y aquí va lo que no habías nombrado.** Me pediste confirmar que no queda ningún
efecto sin considerar, así que no puedo decir «los dos frenos» y callarme el
tercero: existe `integrations.telegram.send_enabled`, en el `config.yaml`, junto
al de Hevy. **Está en `true`** —o sea que no cambia nada de lo de arriba— pero
conviene saber que está, por dos razones.

La primera es que es el único de los tres que **empeora** el silencio: con
`send_enabled: false` el sistema **sigue escribiendo en Hevy** y no manda nada.
La rutina te cambia y no te enteras. Los otros dos van en la dirección contraria
—cortan el efecto y avisan—, así que el reflejo de «apagar el interruptor para
estar más seguro» es justo el equivocado con éste.

La segunda es que sale en `/api/health` como `writes.telegram_send_enabled`, o
sea que ya lo estás mirando en la tabla de §0 aunque no lo supieras. Si algún día
dejan de llegar mensajes y el sistema por lo demás está sano, es el primer sitio
donde mirar.

### 2.4 Habla con Garmin

Sólo lee: métricas de bienestar y actividades. Nunca escribe nada en tu cuenta.

Lo que sí escribe es **en disco**: los tokens de sesión en
`data/garmin_tokens/`. Se reusan entre arranques a propósito, porque el login es
la llamada que provoca los `429 IP rate limited`.

### 2.5 Escribe en disco (dentro del volumen)

| Qué | Dónde |
|---|---|
| Base de datos | `app.db` |
| Caché de salidas | `cache/activities.json` |
| Tokens de Garmin | `garmin_tokens/` |
| Copias previas a cada PUT | `hevy_backups/<routine_id>/` |
| Marca de escritura en curso | se crea y se borra en cada PUT |

### 2.6 Escucha en la red

El 8317 está abierto **a toda la red de casa y sin contraseña**, a propósito.
Eso significa que cualquier cacharro del wifi puede:

- pedir `GET /api/export` — el histórico entero: sueño, HRV, RPE y el registro
  de la lumbar;
- llamar a `POST /api/checkin`, que **no sólo lee**: crea un check-in, dispara
  una decisión, **reescribe la rutina de Hevy y manda el Telegram**.

Con los frenos cerrados esto era feo. Con los frenos abiertos, el peor caso
cambia de «alguien ve mis datos» a «alguien me cambia el entreno». No es motivo
para cerrarlo hoy —es tu red, un solo usuario— pero es el efecto que más se
mueve al abrir los frenos y no estaba escrito en ninguna parte.

El 8000 de la aplicación se queda atado a `127.0.0.1`.

### 2.7 Lo que NO hace

No escribe en Garmin. No manda correo. No publica nada. No llama a ninguna IA:
el motor son reglas explícitas del `config.yaml`. No borra copias salvo que
`backup_keep_last` esté puesto, y por defecto no lo está.

---

## 3. Revertir una rutina

El comando, probado el sábado 12 contra el contenedor que corre y contra la
Hevy de verdad.

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -m app.rutina revertir --rutina dia_1
```

Te enseña qué copia va a usar, te avisa de que el PUT reemplaza la rutina
entera, y **te hace escribir `REVERTIR`** para continuar. Con `--si` no pregunta
(para cuando ya sabes lo que haces, o desde un script).

```
rutina : dia_3 (05e27b0c-7c9e-484c-a9aa-1c78622bc0c2)
copia  : 20260912-135152.json (11 ejercicios, 2026-09-12 13:51:52)
         /app/data/hevy_backups/.../20260912-135152.json

Esto hace un PUT en Hevy y REEMPLAZA la rutina entera por el
contenido de esa copia. Lo que haya ahora se pierde.

El interruptor `write_enabled` NO se consulta: revertir tiene que
funcionar aunque el de escritura esté apagado.

Escribe REVERTIR para continuar:
```

Dos cosas que importan:

- **Revertir ignora `write_enabled` a propósito.** Si algo se rompió con el
  interruptor abierto, se tiene que poder deshacer aunque lo cierres después.
- Sin `--copia` usa **la más reciente**, que es la del estado inmediatamente
  anterior a la última escritura. Para ir más atrás:
  `--copia 20260912-134316.json`.

Ver qué copias hay:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -m app.rutina copias --rutina dia_1
```

### La prueba de ida y vuelta que se hizo

No es una promesa: es lo que pasó.

```
1. ANTES   : 11 ejercicios · «Día 3»
2. ESCRITO : PUT destructivo -> 2 ejercicios · «Dia 3 -- PRUEBA DE REVERTIDO»
3. REVERTIR: PUT desde la copia
4. AHORA   : 11 ejercicios · «Día 3»
5. HUELLA  : idéntica a la de la copia, campo a campo
```

Lo único que cambia entre la copia y la rutina revertida es `updated_at`, que lo
pone Hevy en cada escritura y no es algo que se pueda ni se deba reproducir.

**Esa prueba encontró que el revertido no funcionaba.** Devolvía `400
Unrecognized key(s) in object: 'index'`. La copia es la respuesta de un `GET`
tal cual, y el `GET` de Hevy devuelve campos (`index`, `title` de cada
ejercicio) que el `PUT` rechaza. El camino de escritura del lunes tenía el mismo
defecto exacto, así que la primera escritura real tampoco habría funcionado. Los
seis tests que cubrían el revertido estaban en verde: usaban un doble de HTTP
que aceptaba cualquier cuerpo. Ya está arreglado y el doble ahora reproduce el
400 de Hevy.

---

## 4. Qué se escribió, y qué había antes

Dos comandos, y vienen de sitios distintos a propósito: lo escrito está en la
base de datos y lo anterior está en un fichero. Juntarlos en una sola fuente
significaría que una base corrupta se lleva las dos cosas a la vez.

**Un vistazo a todo:**

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -m app.rutina estado
```

```
copias en: /app/data/hevy_backups

no hay escrituras a medias

rutina         copias  última copia
--------------------------------------------------------------
dia_1               0  — nunca escrita —
dia_2               0  — nunca escrita —
dia_3               3  2026-09-12 13:51:52
```

Si en vez de «no hay escrituras a medias» sale **`!! HAY UNA ESCRITURA SIN
CONFIRMAR`**, significa que el proceso murió entre el PUT y su confirmación: la
rutina puede ser la vieja, la nueva o una mezcla. Se mira en Hevy antes de nada.

**El detalle de una rutina:**

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -m app.rutina ver --rutina dia_1
```

Enseña las últimas escrituras con su estado (`ok`, `error`, `read_only`,
`dry_run`, `reverted`, `stale`) y el motivo de cada una —los intentos fallidos
también se registran—, y debajo el **diff entre la copia previa y lo que se
escribió**: qué ejercicio cambió, qué series, qué kilos.

Los dos últimos estados son del **check-in tardío**. Si el trabajo de respaldo
de las 09:00 escribió una rutina y luego el formulario cambia la decisión a un
día que no toca Hevy, la escritura de la mañana se deshace: eso es `reverted`, y
el día queda con dos filas —la que puso la rutina y la que la quitó— para que la
secuencia se pueda reconstruir. `stale` es cuando había que deshacerla y no se
pudo: en Hevy ha quedado una sesión que hoy no toca, y el Telegram de ese día lo
dice arriba del todo con el nombre de las dos rutinas.

**El texto exacto del Telegram que se envió:**

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -c "
import sqlite3
c = sqlite3.connect('/app/data/app.db')
f = c.execute('select date, status, body from notifications order by id desc limit 1').fetchone()
print('sin avisos todavía' if f is None else f'{f[0]} [{f[1]}]\n\n{f[2]}')
"
```

Esa fila se escribe **siempre**, también los días en los que no se envía nada: si
se saltara, en el histórico no se distinguiría un día sin mensaje de un día sin
decisión.

**Sacar las copias a Windows** (el volumen no se ve desde el explorador):

```bash
docker cp adaptive-training:/app/data/hevy_backups ./hevy_backups
```

---

## 5. El domingo 13: el ensayo gratis

El domingo es día de **bici**. Eso significa que a las 09:00 el
`decision_fallback` va a decidir, **va a mandar un Telegram de verdad**, y **no
va a tocar Hevy** (los días de bici no escriben rutina).

Es el ensayo que no había manera de comprar: prueba la mitad de Telegram sin
arriesgar la de Hevy.

Lo que hay que hacer el domingo son dos cosas:

1. **Que llegue el mensaje.** Si a las 09:05 no ha llegado nada, algo está mal y
   quedan 24 horas para arreglarlo. Empieza por `/api/health`.
2. **Dos minutos de comprobación** (o el domingo por la noche):

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml ps
curl -s localhost:8317/api/health | python -m json.tool
```

Los dos contenedores en `Up`, y la tabla del §0 entera.

El mensaje del domingo dirá que el programa todavía no ha empezado. Es correcto:
el motor no está dormido antes del lunes, simplemente el programa arranca el 14.

---

## 5 bis. Dos cosas que aparecieron el domingo 13 por la tarde

Las dos son del mismo tipo: **el contenedor contestaba `status: ok` con los dos
problemas puestos**. Ninguna habría dado la cara por sí sola.

### a) El contenedor lleva un `config.yaml` que no es el del disco

`get_config()` carga el YAML **una vez por proceso** y lo cachea. El contenedor
arrancó el sábado 12 a las 12:04; el `config.yaml` se ha editado después. O sea
que lo que decide no es lo que pone el fichero:

| dónde | hash |
|---|---|
| `config.yaml` en disco | `02f523b6fe031eec` |
| lo que sirve el contenedor en `/api/health` | `8a0e1e304324a6be` |

Y hay una segunda capa, peor: el **código** del contenedor también es el del
sábado, y su validador **rechaza** el `config.yaml` de ahora —no conoce la
sección `metrics`—. Mientras nadie recargue, sigue funcionando con el de
memoria. El día que el proceso se reinicie solo, arranca contra un YAML que su
propio validador tira.

**Hay que reconstruir antes del lunes.** No es opcional y no basta con
reiniciar:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml up -d --build
curl -s localhost:8317/api/health | python -m json.tool   # el hash tiene que ser el del disco
```

Comparar el `config_hash` con el del disco es la comprobación; el `status: ok`
no lo es, porque salía `ok` con el config viejo puesto.

### b) El directorio de tokens de Garmin no se podía escribir

`data/garmin_tokens` estaba en modo `111` (`d--x--x--x`): ni listable ni
escribible **por el propio usuario del contenedor**. Los hermanos (`cache`,
`hevy_backups`) están en 777, así que parece un accidente de creación.

Importa porque `Garmin.login()` guarda los tokens dentro de un
`contextlib.suppress(Exception)`: si el directorio no deja escribir, **el login
sale perfectamente correcto y no guarda nada**. No falla; encarece. Cada
ejecución repite el login entero con su cadena de estrategias y sus 429, contra
un servicio que corta por IP, y cada ejecución por separado parece bien.

Ya está arreglado en el volumen que usa el contenedor (`chmod 700`), y
verificado: dos ejecuciones seguidas reanudan sesión sin gastar login.

> **Ojo con cuál es cuál.** El compose de `pruebas-lan` monta `/app/data` como
> **volumen nombrado** (`hevy2garmin-test_datos-pruebas`), no como `./data`. Son
> dos almacenes de tokens distintos. El arreglado es el del volumen, que es el
> que usa el contenedor. El de `./data` —el que usaría el compose de Umbrel, y
> el que usan los scripts lanzados a mano desde Windows— **sigue roto**, y ahí
> el problema es una ACL de Windows que niega el acceso hasta para leerla.

De esto sale además una comprobación nueva que antes no existía: después de un
login con credenciales, el cliente mira si quedó algo escrito y lo dice. No
revienta —no poder guardar la sesión encarece mañana, no impide leer hoy, y
convertirlo en excepción cambiaría una degradación por una avería total—, pero
deja un `ERROR` en el log y un paso en rojo en el botón de Garmin.

---

## 6. El lunes 14

### 6.1 Al levantarte — el check-in, en el móvil

**`http://192.168.8.201:8317`**

Siete deslizadores:

| Campo | Qué es |
|---|---|
| Cansancio general | `fatigue` |
| Ánimo | `mood` |
| Molestias tronco superior | cervicales y hombros |
| **Molestias tronco inferior** | **la lumbar** |
| Calidad del sueño percibida | lo que tú notas, no lo que dice el reloj |
| Ganas de entrenar | `training_desire` |
| Esfuerzo del entreno de ayer | RPE |

**Hazlo antes de las 09:00, y hazlo entero.** Con check-in completo el motor
evalúa todas las reglas; sin él **se quedan once sin evaluar por falta de
datos**, incluida `lumbar_alto`, que es la que más te importa. El mensaje lo dice
—*«Decidido con datos incompletos»*— pero un aviso que aparece todos los días
deja de leerse en una semana.

A las 09:00 salta `decision_fallback`, que decide **sólo si no hay check-in**. No
pisa el tuyo.

**Enviar el formulario dispara la decisión en el momento**, y con los frenos
abiertos eso significa que **en ese momento se reescribe la rutina de Hevy y sale
el Telegram**. No hay que esperar a ninguna hora.

### 6.2 Lo que el motor va a decidir — y lo que no se puede saber desde aquí

Lo que **sí** está determinado antes del lunes:

- **Sesión: `dia_1`**, tren inferior + core. Rutina de Hevy
  `29ce5818-5442-4a40-9e70-1e74904d5867`.
- **Descarga: no.** La primera semana de descarga empieza el **2026-11-02**,
  siete semanas exactas después del arranque.
- Va a aparecer **«Progresión cerrada: no hay registro de la última sesión con
  el que comparar»**, y es correcto: no hay sesión anterior que reconciliar.
  Desaparece sola el día 2.

Lo que **no** se puede predecir desde aquí, y por eso no se predice:

- **El semáforo.** Ensayado el sábado con el config real, el lunes salía **ámbar
  por `resaca_finde`** (1 salida intensa y 2,71 h en el fin de semana). Pero ese
  ensayo lee el fin de semana que **ya ha pasado**, no el que tienes por delante.
  El lunes la regla mirará tus salidas del sábado 12 y el domingo 13, que aún no
  existen.
- Y el semáforo decide la sesión: en ámbar, `dia_1` se reduce de 9 ejercicios a
  6 y las series bajan de 3 a 2.

O sea: **si este fin de semana sales en bici parecido a como saliste el
anterior, el lunes va a salir ámbar y sesión reducida.** No es una avería, es la
regla haciendo su trabajo. El bloque **«Por qué»** del mensaje dice siempre cuál
saltó y con qué números.

### 6.3 Después del primer mensaje: comprobar Hevy

Esto es lo único que hay que hacer el lunes que no se hace ningún otro día.

**Abre Hevy y la rutina «Día 1». Compárala con el Telegram, en este orden:**

1. **¿Cuántos ejercicios?** El mensaje los lista uno por uno. Tienen que estar
   todos y en el mismo orden.
2. **¿Cuántas series por ejercicio?** El mensaje escribe `2×12 · 35 kg`: dos
   series. Y `cal. 1×10 · 40 kg | 1×12 · 70 kg + 1×12 · 80 kg` son tres: una de
   calentamiento y dos efectivas.
3. **¿Los kilos?** Ejercicio por ejercicio.
4. **Si el mensaje dice «Fuera hoy: …»**, esos ejercicios **no** pueden estar en
   la rutina. Es la comprobación que más rápido delata una escritura a medias.

**Si cuadra**, ya está. No hay que volver a mirarlo ningún otro día: el lunes se
mira porque es la primera escritura real que hace este sistema en su vida.

**Si no cuadra:**

*Caso A — el mensaje ya lo avisa.* Arriba del todo dice **«la rutina NO se ha
escrito en Hevy»** y la segunda línea da el motivo. Entonces la rutina que ves es
la anterior y el mensaje describe una sesión que en Hevy no está. Entrena por el
mensaje, no por la app, y mira el motivo:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -m app.rutina ver --rutina dia_1
```

*Caso B — el mensaje no avisa de nada y la rutina es la vieja.* Eso es lo que
no debería poder pasar. `rutina ver` dirá qué estado quedó registrado. Si dice
`ok`, mira la fecha y la hora: puede que estés viendo Hevy sin refrescar.

*Caso C — la rutina está a medias o irreconocible.* Mira si hay una escritura
sin confirmar:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -m app.rutina estado
```

Y déjala como estaba:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -m app.rutina revertir --rutina dia_1
```

Eso la devuelve al estado **anterior a la escritura de esta mañana**, que es la
rutina que tenías el domingo. Entrena por el mensaje de Telegram, que sigue
siendo la decisión buena.

*En cualquiera de los tres casos*: el entrenamiento del lunes no se pierde. La
decisión está tomada y escrita en la base; lo que puede fallar es el transporte
hasta Hevy, y el mensaje tiene el plan entero.

### 6.4 Después de entrenar

Registra la sesión en Hevy como siempre. A las **22:30** el trabajo `reconcile`
lee lo que hiciste de verdad y lo compara con lo planeado. De ahí sale la
progresión del día siguiente.

---

## 7. Los días siguientes: la única pregunta que hay que contestar

Sin fase en seco, la pregunta no cambia; sólo cambia que ahora la respuesta
cuesta algo:

> **¿Lo que decide coincide con lo que yo habría hecho?**

Cada mañana, antes de mirar la pantalla, decide tú. Después mira. Y anota los
desacuerdos, **sobre todo el sentido**: si el sistema es más conservador que tú,
o al revés. Un sistema que siempre frena de más se acaba ignorando, y ése es el
fallo que no avisa.

**Los umbrales no se tocan por una mañana rara.** Si a la semana ves que uno está
mal, se anota y se decide después, con la serie delante. Cambiar un umbral el
tercer día es ajustar el motor al ruido.

---

## 8. Las fechas que ya están puestas

| Cuándo | Qué |
|---|---|
| **2026-09-14** | `program.start`. Lunes, y ahora es obligatorio que lo sea |
| **~2026-10-12** | Cuatro semanas desde `recalibrado_el`: salta el aviso de recalibración |
| **2026-11-02** | Primera semana de descarga, a siete semanas exactas |

El aviso de recalibración cuenta **días con decisión**, no días de calendario. Si
el sistema estuvo parado una semana, esa semana no cuenta. Por eso no se puede
predecir la fecha exacta desde aquí.

---

## 9. Si algo va mal

Las averías de abajo **ya han pasado de verdad**. No son hipótesis.

### «La rutina de Hevy ha quedado mal»

§3. El revertido está probado de ida y vuelta.

### «El contenedor usa la base equivocada / faltan las copias de Hevy»

Se levantó sin los dos `-f`. §0. Se comprueba con `auth_front` en el health y se
confirma con:

```bash
docker inspect adaptive-training --format '{{range .Mounts}}{{.Type}} {{.Name}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
```

Tiene que salir `volume hevy2garmin-test_datos-pruebas -> /app/data`. Si sale un
`bind ...\data`, es el compose equivocado.

### «El contenedor reinicia en bucle»

```bash
docker logs --tail 40 adaptive-training
```

Si dice `SchemaDesfasado`, la base tiene columnas que el modelo ya no declara y
**se para a propósito**: seguir significaría reventar más tarde, de noche y sin
nadie delante. Hay que migrarla a mano o poner una base que case con el modelo.

Si dice `disk I/O error`, es el bind mount de Windows con SQLite en WAL: otra
vez el compose equivocado.

### «El 8317 pide contraseña / da 502»

El proxy tiene cargada una configuración vieja. `caddy reload` **no sirve**
(`admin off`). Recrear:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  up -d --force-recreate proxy
```

Ojo: eso arrastra al contenedor de la aplicación, que se reiniciará. Si la imagen
está desfasada, es justo cuando se descubre.

### «Garmin devuelve 429»

*IP rate limited*. Pasa al hacer login muchas veces seguidas. El contenedor no lo
sufre porque reusa el token de `data/garmin_tokens`, pero **el CLI del PC sí lo
provoca** si se lanza repetidamente. Para ensayar sin tocar Garmin:

```bash
./.venv/Scripts/python.exe -m app.cli --date 2026-09-14 --dry-run --offline \
  --checkin "fatigue=4,mood=7,upper=1,lower=2,sleep=7,desire=8,rpe=5"
```

`--offline` usa datos de ejemplo **marcados como inventados** en la cabecera del
informe. Sirve para ver el camino completo, no para decidir si entrenar hoy.

El ensayo en seco del CLI **nunca escribe en Hevy ni envía nada**, con los frenos
como estén. Enseña lo que escribiría, incluido el cuerpo del PUT entero.

### Sacar la base del contenedor para mirarla en el PC

```bash
docker cp adaptive-training:/app/data/app.db ./app.db
```

**Con el contenedor parado.** Si está vivo, la mayor parte de los datos recién
escritos está en el `-wal` y no en el `.db`, y lo que sale es una base incompleta
sin avisar de nada.

---

## 10. Mover una base al volumen: el paso que muerde

Está aquí porque se ha hecho dos veces y porque **la forma evidente de hacerlo
destruye los datos en silencio**, que es la peor manera de destruirlos.

El volumen es `hevy2garmin-test_datos-pruebas`, no una carpeta del PC (ver §0).

**Lo que parece que hay que hacer:** copiar el `app.db` bueno encima del malo.

**Lo que pasa entonces, dos veces, en las dos direcciones:**

- SQLite encuentra el `app.db-wal` del contenedor anterior, lo da por válido y lo
  reproduce encima de la base recién copiada. Sin error, sin aviso. Probado sobre
  un volumen de usar y tirar: se copió una base de **179 días** y al abrirla tenía
  **9**. Los del contenedor viejo.
- Y al revés: parar el contenedor **no** volcó su WAL en el `app.db`. Copiar el
  `.db` solo habría perdido todo lo escrito ese día.

**El procedimiento correcto**, con el contenedor **parado**:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml stop app

# 1. volcar el WAL del origen en su .db (el paso invisible)
./.venv/Scripts/python.exe -c "
import sqlite3
c = sqlite3.connect('data/app.db')
print(c.execute('pragma wal_checkpoint(TRUNCATE)').fetchone())
"

# 2. copiar, borrando ANTES el WAL del destino
docker run --rm \
  -v hevy2garmin-test_datos-pruebas:/vol \
  -v "D:/hevy2garmin-test/data":/pc:ro \
  alpine sh -c '
    rm -f /vol/app.db-wal /vol/app.db-shm     # <-- ESTO es el paso
    cp /pc/app.db /vol/app.db
    mkdir -p /vol/cache         && cp -a /pc/cache/. /vol/cache/
    mkdir -p /vol/garmin_tokens && cp -a /pc/garmin_tokens/. /vol/garmin_tokens/
    mkdir -p /vol/hevy_backups  && cp -a /pc/hevy_backups/. /vol/hevy_backups/
    chown -R 1000:1000 /vol
  '

docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  up -d --force-recreate app
```

Tres cosas que no se ven en el comando:

- **`chown 1000:1000`.** El contenedor corre como `app` (uid 1000). Una base
  propiedad de `root` se deja leer y no se deja escribir, así que el fallo no
  aparece al arrancar: aparece en la primera escritura, horas después.
- **`hevy_backups/` va también.** Es lo único que permite deshacer una escritura
  en Hevy. Olvidarlo deja el sistema escribiendo sin red.
- **Comprobar siempre después**, porque el fallo de arriba es mudo:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -c "
import sqlite3
c = sqlite3.connect('/app/data/app.db')
print(c.execute('select count(*) from daily_metrics').fetchone()[0],
      c.execute('select min(date), max(date) from daily_metrics').fetchone())
"
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml \
  exec app python -m app.rutina estado
```

---

## 11. Cabo suelto

**`.env.pruebas-lan` es un huérfano.** Contiene `LOCAL_USER` y un hash de bcrypt
—`LOCAL_PASSWORD_HASH`— para el `basic_auth` que se quitó del `Caddyfile`. Ya no
lo lee nadie. Está en `.gitignore`, así que no viaja, pero sigue en el disco.

No se ha borrado a propósito: es un fichero de credenciales y eso no se tira sin
que lo mires. **Bórralo tú** cuando quieras.
