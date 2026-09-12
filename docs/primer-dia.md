# El primer día

Esto no es el manual de instalación —ése está en el [README](../README.md#instalación)
y sirve para montar el sistema desde cero en una máquina vacía—. Esto es el
guión del **lunes 14 de septiembre de 2026**, que es el día que `program.start`
señala y el primero en el que el sistema decide algo que va a gobernar un
entrenamiento de verdad.

La diferencia importa. Instalar es un problema resuelto; lo que no está resuelto
es **qué mira uno el primer día para saber si lo que ha arrancado está vivo o
solo parece vivo**, que no es lo mismo y desde fuera se ven igual.

---

## 0. Lo que pasó el sábado 12, y por qué esta guía existe

Hay que empezar por aquí porque explica la mitad de los pasos.

El contenedor que llevaba veinticinco horas en marcha, sano, contestando `ok` en
`/api/health`, **estaba a un reinicio de no volver a arrancar**. No por un fallo
nuevo: por tres desfases acumulados entre lo que había en disco y lo que había
cargado en memoria.

1. **La imagen era vieja.** Se construyó el 10 de septiembre. Desde entonces se
   borraron del modelo tres columnas (`readiness`, `load_3d`, `load_7d`) y se
   cambió el `config.yaml`. El contenedor seguía funcionando porque un proceso
   ya arrancado no vuelve a leer nada de eso.
2. **La base del contenedor tenía esas tres columnas con datos.** La guarda de
   esquema —la que existe justamente para que la persistencia muera en voz alta
   en vez de degradarse callando— se negó a arrancar: *«sobra, pero tiene 9
   valores no nulos. No se borra sola: eso puede ser la única copia»*.
3. **La contraseña que quitaste seguía viva.** `Caddyfile.pruebas` ya no tiene
   `basic_auth`, el cambio está commiteado y el fichero está montado dentro del
   proxy —se puede leer allí, sin `basic_auth`—, pero **Caddy cargó su
   configuración al arrancar y no vuelve a mirar el fichero**. El puerto 8317
   seguía devolviendo 401 contra un fichero que ya no pedía nada.

Los tres son la misma avería con tres caras: *el valor que se lee no es el valor
que se usa*. Es la figura que se repite en este proyecto, y no falla nunca
mientras nadie reinicie.

Se descubrió porque recrear el proxy arrastró al contenedor de la aplicación, y
entonces sí se cayó. **Ya está arreglado**: imagen reconstruida, base
trasplantada, proxy recreado. Pero conviene saber que el estado «sano» de un
contenedor sin reiniciar no dice nada sobre si arrancaría otra vez.

> **Lo que hay que llevarse de aquí:** después de cualquier cambio en
> `config.yaml`, en el `Caddyfile` o en el modelo de la base, **recrear los
> contenedores**. No basta con guardar el fichero, y el sistema no se queja
> hasta el siguiente arranque, que puede ser un corte de luz a las tres de la
> mañana.

Y un detalle operativo que cuesta encontrar: **el `Caddyfile` no se puede
recargar en caliente**. `caddy reload` falla con `connection refused` porque el
propio fichero pone `admin off`, que apaga la API de administración a propósito.
La única forma de aplicar un cambio es recrear el contenedor.

---

## 1. Estado en el que te lo dejo (sábado 12, 09:12)

Comprobado, no supuesto:

| Cosa | Valor |
|---|---|
| Imagen del contenedor | `adaptive-training:0.1.0`, reconstruida hoy |
| `config_hash` | `3929d72e49c5fb2d` |
| `daily_metrics` | **180 filas**, 2026-03-15 → 2026-09-10 |
| `activities` | 58 |
| `decisions` | 0 — ninguna decisión tomada todavía |
| `cache/activities.json` | 411 KB, 184 días de salidas |
| `garmin_tokens/` | intacto; la sesión se reanuda sin volver a hacer login |
| Puerto 8317 | 200, **sin contraseña**, a propósito |
| `DRY_RUN` | `true` |
| `integrations.hevy.write_enabled` | `false` |

Los 179 días de histórico que estaban en el PC y **no** estaban en el contenedor
—el contenedor tenía 9— ya están dentro. El trabajo de arranque
`backfill_wellness` se encargó solo del día que faltaba: encontró el hueco,
lo pidió a Garmin reusando el token guardado y lo escribió, 90 segundos después
de levantarse.

La base anterior del contenedor no se ha tirado. Está en
`data/copias/app.db.contenedor-antes-del-trasplante-2026-09-12`, con sus 2
decisiones y sus días de bienestar del 11 y el 12. No hace falta para nada, pero
borrar la única copia de algo nunca es un paso de una guía.

---

## 2. El domingo 13 por la noche: dos minutos

Nada más que esto, y solo para no descubrir el lunes a las siete de la mañana
algo que se podía haber visto el domingo.

```bash
cd D:/hevy2garmin-test
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml ps
curl -s localhost:8317/api/health | python -m json.tool
```

Los dos contenedores en `Up`, y en el JSON **las tres cosas de siempre**, que no
son el `status`:

- **`secrets_missing` vacío.** Si falta una clave, la aplicación arranca igual
  —para poder abrir el formulario y ver qué falta— y luego no hace su trabajo.
- **`scheduler.running: true` con los trabajos y su hora.** Sin planificador, la
  aplicación sirve el formulario, contesta `ok` y no decide nunca. Desde fuera
  eso se parece muchísimo a una semana de descanso.
- **`clock.matches: true`.** Es el reloj del proceso contra la zona del
  `config.yaml`. Si no coinciden, un check-in de madrugada se guarda con la
  fecha de ayer y por la mañana se decide como si no lo hubiera habido.

El domingo el `decision_fallback` de las 09:00 habrá tomado una decisión de
domingo. Es normal y no estorba: el programa arranca el lunes, pero el motor no
está dormido antes.

---

## 3. El lunes 14

### 3.1 Al levantarte — el check-in, en el móvil

**`http://192.168.8.201:8317`**

Es el formulario. Siete deslizadores:

| Campo | Qué es |
|---|---|
| Cansancio general | `fatigue` |
| Ánimo | `mood` |
| Molestias tronco superior | cervicales y hombros |
| **Molestias tronco inferior** | **la lumbar** |
| Calidad del sueño percibida | lo que tú notas, no lo que dice el reloj |
| Ganas de entrenar | `training_desire` |
| Esfuerzo del entreno de ayer | RPE |

**Hazlo antes de las 09:00, y hazlo entero.** No es cosmética: con check-in
completo el motor evalúa todas las reglas; sin él **se quedan once sin evaluar
por falta de datos**, incluida `lumbar_alto`, que es la que más te importa. El
mensaje lo dice cuando pasa —*«Decidido con datos incompletos»*— pero un aviso
que aparece todos los días deja de leerse en una semana.

A las 09:00 salta `decision_fallback`, que decide **solo si no hay check-in**.
No pisa el tuyo. Es la red por debajo, no la vía normal.

Enviar el formulario **dispara la decisión en el momento**. No hay que esperar.

### 3.2 Lo que vas a ver — y lo que NO

En la PWA: el semáforo y la sesión del día, ejercicio a ejercicio con sus series
y sus kilos.

**En Telegram no va a llegar nada.** Con `DRY_RUN=true` el mensaje se compone
entero, se guarda entero y **no se envía**. Es lo correcto para esta fase y es
justamente lo que más se parece a una avería, así que conviene tenerlo claro de
antemano: *no llega mensaje* significa hoy «está funcionando», y el día que
quites `DRY_RUN` pasará a significar lo contrario.

El texto exacto que se habría enviado se puede leer:

```bash
docker exec adaptive-training python -c "
import sqlite3
c = sqlite3.connect('/app/data/app.db')
f = c.execute('select date, status, body from notifications order by id desc limit 1').fetchone()
print('sin avisos todavía' if f is None else f'{f[0]} [{f[1]}]\n\n{f[2]}')
"
```

El `status` dirá `dry_run`. Esa fila se escribe **siempre**, también los días en
los que no hay a quién avisar: si se saltara, en el histórico no se distinguiría
un día sin mensaje de un día sin decisión.

### 3.3 Lo que el motor va a decidir el lunes

Ensayado hoy con el config real y los datos reales:

```
🟢 Lunes 14 de septiembre — VERDE
💪 Día 1 (sesión completa) — Tren inferior + core
   Prensa horizontal · Extensión de cuádriceps · Patada atrás ·
   Elevación de gemelo · Press de tríceps · Elevación de cadera a una pierna ·
   Plancha lateral · Perro de caza · Bosu propiocepción
```

- **Descarga: no.** La siguiente semana de descarga empieza el **2026-11-02**,
  que son siete semanas exactas después del arranque. Ese «exactas» es nuevo:
  antes salían 6,86 porque el origen se redondeaba al lunes anterior sin
  decirlo.
- **«Progresión cerrada: no hay registro de la última sesión con el que
  comparar»** va a aparecer, y es correcto. No hay sesión anterior que
  reconciliar. Desaparece sola el día 2.

Si el lunes ves algo muy distinto de esto, no es que la guía esté mal: es que
una regla ha saltado con tus datos de esa mañana, que es exactamente para lo que
está el sistema. El bloque **«Por qué»** del mensaje dice cuál.

### 3.4 Después de entrenar

Registra la sesión en Hevy como siempre. A las **22:30** el trabajo
`reconcile` lee lo que hiciste de verdad y lo compara con lo que se planeó. De
ahí sale la progresión del día siguiente.

Esta parte **no** depende de `DRY_RUN`: leer Hevy se hace igual. Lo que está
apagado es escribir.

---

## 4. Los días siguientes: la única pregunta que hay que contestar

La fase en seco tiene un solo propósito, y no es comprobar que el sistema
funciona —eso lo dicen los tests—. Es contestar:

> **¿Lo que decide coincide con lo que yo habría hecho?**

Cada mañana, antes de mirar la pantalla, decide tú. Después mira. Y anota los
desacuerdos, sobre todo el sentido: si el sistema es más conservador que tú, o
al revés. Un sistema que siempre frena de más se acaba ignorando, y ése es el
fallo que no avisa.

**Los umbrales no se tocan durante esta fase.** Si a la semana ves que uno está
mal, se anota y se decide después, con la serie delante. Cambiar un umbral el
tercer día por una mañana rara es ajustar el motor al ruido.

---

## 5. Cuándo quitar los frenos, y en qué orden

Son **dos interruptores independientes**, y ésa es la gracia: se pueden soltar
de uno en uno.

| Interruptor | Dónde | Qué suelta |
|---|---|---|
| `DRY_RUN=false` | `.env` | Telegram empieza a enviar |
| `integrations.hevy.write_enabled: true` | `config.yaml` | Empieza a reescribir la rutina |

**El orden recomendado es ése, y con días de por medio.** Telegram primero
porque su peor caso es un mensaje de más. El de Hevy es otra cosa: `PUT
/v1/routines/{id}` **reemplaza la rutina entera**, así que el coste de una
decisión mal construida no es un mal día de entreno, es una rutina destruida.
Antes de cada escritura se hace copia, y sin copia verificada no se escribe
—eso está en el código, no en el config—, pero la asimetría sigue ahí.

Los dos cambios exigen **recrear el contenedor**. Ver el punto 0.

---

## 6. Las fechas que ya están puestas

| Cuándo | Qué |
|---|---|
| **2026-09-14** | `program.start`. Lunes, y ahora es obligatorio que lo sea |
| **~2026-10-12** | Cuatro semanas desde `recalibrado_el`: salta el aviso de recalibración |
| **2026-11-02** | Primera semana de descarga, a siete semanas exactas |

El aviso de recalibración cuenta **días con decisión**, no días de calendario.
Si el sistema estuvo parado una semana, esa semana no cuenta. Por eso no se
puede predecir la fecha exacta desde aquí.

---

## 7. Si algo va mal

Las tres averías de abajo **ya han pasado de verdad**. No son hipótesis.

### «El contenedor reinicia en bucle»

```bash
docker logs --tail 40 adaptive-training
```

Si dice `SchemaDesfasado`, la base tiene columnas que el modelo ya no declara y
**se para a propósito**: seguir significaría reventar más tarde, de noche y sin
nadie delante. Hay que migrarla a mano o poner una base que case con el modelo.

### «El 8317 pide contraseña / da 502»

El proxy tiene cargada una configuración vieja. `caddy reload` **no sirve**
(`admin off`). Recrear:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml up -d --force-recreate proxy
```

Ojo: eso arrastra al contenedor de la aplicación, que se reiniciará. Si la
imagen está desfasada, es justo cuando se descubre.

### «Garmin devuelve 429»

*IP rate limited*. Pasa al hacer login muchas veces seguidas. El contenedor no
lo sufre porque reusa el token de `data/garmin_tokens`, pero **el CLI del PC sí
lo provoca** si se lanza repetidamente. Para ensayar sin tocar Garmin:

```bash
./.venv/Scripts/python.exe -m app.cli --date 2026-09-14 --dry-run --offline \
  --checkin "fatigue=4,mood=7,upper=1,lower=2,sleep=7,desire=8,rpe=5"
```

`--offline` usa datos de ejemplo **marcados como inventados** en la cabecera del
informe. Sirve para ver el camino completo, no para decidir si entrenar hoy.

### Sacar la base del contenedor para mirarla en el PC

```bash
docker cp adaptive-training:/app/data/app.db ./app.db
```

**Con el contenedor parado.** Si está vivo, la mayor parte de los datos recién
escritos está en el `-wal` y no en el `.db`, y lo que sale es una base
incompleta sin avisar de nada.

---

## 8. Trasplantar una base al contenedor: el paso que muerde

Está aquí porque hoy se ha hecho y porque **la forma evidente de hacerlo
destruye los datos en silencio**, que es la peor manera de destruirlos.

El volumen es `hevy2garmin-test_datos-pruebas`, no una carpeta del PC. Es a
propósito: en Windows, un *bind mount* con SQLite en modo WAL da `disk I/O
error` y deja el contenedor en bucle.

**Lo que parece que hay que hacer:** copiar el `app.db` bueno encima del malo.

**Lo que pasa entonces:** SQLite encuentra el `app.db-wal` del contenedor
anterior, lo da por válido y lo reproduce encima de la base recién copiada. Sin
error, sin aviso, sin nada. Probado hoy sobre un volumen de usar y tirar: se
copió una base de **179 días** y al abrirla tenía **9**. Los del contenedor
viejo. La base trasplantada había desaparecido y el proceso había terminado con
éxito.

**El procedimiento correcto**, con el contenedor **parado**:

```bash
docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml stop app

docker run --rm \
  -v hevy2garmin-test_datos-pruebas:/vol \
  -v "D:/hevy2garmin-test/data":/pc:ro \
  alpine sh -c '
    rm -f /vol/app.db-wal /vol/app.db-shm     # <-- ESTO es el paso
    cp /pc/app.db /vol/app.db
    mkdir -p /vol/cache && cp -a /pc/cache/. /vol/cache/
    chown -R 1000:1000 /vol
  '

docker compose -f docker-compose.yml -f docker-compose.pruebas-lan.yml up -d --force-recreate app
```

Tres cosas que no se ven en el comando:

- **`chown 1000:1000`.** El contenedor corre como `app` (uid 1000). Una base
  propiedad de `root` se deja leer y no se deja escribir, así que el fallo no
  aparece al arrancar: aparece en la primera escritura, que puede ser horas
  después.
- **No se borra `garmin_tokens/`.** Si desaparece, el arranque vuelve a hacer
  login y Garmin contesta 429.
- **Comprobar siempre después**, porque el fallo de arriba es mudo:

```bash
docker exec adaptive-training python -c "
import sqlite3
c = sqlite3.connect('/app/data/app.db')
print(c.execute('select count(*) from daily_metrics').fetchone()[0],
      c.execute('select min(date), max(date) from daily_metrics').fetchone())
"
```

---

## 9. Cabo suelto

**`.env.pruebas-lan` es un huérfano.** Contiene `LOCAL_USER` y un hash de bcrypt
—`LOCAL_PASSWORD_HASH`— para el `basic_auth` que se quitó del `Caddyfile`. Ya no
lo lee nadie. Está en `.gitignore`, así que no viaja, pero sigue en el disco.

No se ha borrado a propósito: es un fichero de credenciales y eso no se tira sin
que lo mires. **Bórralo tú** cuando quieras.
