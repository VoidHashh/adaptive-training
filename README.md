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

## Instalación en Umbrel

Umbrel es Debian sobre ARM64 o x86 con Docker. No hace falta nada más.

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
  }
}
```

Mirar esas dos cosas, no solo el `status`:

- **`secrets_missing` vacío.** Si falta algo, la aplicación arranca igual -para
  poder abrir el formulario y ver qué falta- pero no podrá hacer su trabajo.
- **`scheduler.running` en `true` y los tres trabajos con hora.** Sin
  planificador la aplicación sirve el formulario, contesta `ok` y no decide
  nunca. Desde fuera se parece muchísimo a una semana de descanso.

### Las claves

| Variable | De dónde sale |
|----------|---------------|
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | La cuenta de siempre. |
| `HEVY_API_KEY` | Hevy → Ajustes → Developer (necesita Hevy Pro). |
| `TELEGRAM_BOT_TOKEN` | Lo da `@BotFather` al crear el bot. |
| `TELEGRAM_CHAT_ID` | Escribirle al bot y mirar `api.telegram.org/bot<TOKEN>/getUpdates`. |

### Empezar en seco

`DRY_RUN=true` los primeros días. El sistema decide, guarda y registra, pero no
toca Hevy ni manda Telegram. Cuando lo que decida tenga sentido, se pone en
`false` y se reinicia.

### El formulario en el móvil

Abrir `http://umbrel.local:8000` (o el dominio que ponga Umbrel delante) y
"Añadir a pantalla de inicio". Se instala como una aplicación y arranca sin
conexión, aunque **sin red no dice nada del día**: eso es deliberado, porque un
semáforo de ayer pintado como el de hoy no se distingue de la verdad.

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

```bash
git pull
docker compose up -d --build
```

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

## Cómo está montado

```
app/engine/      El motor: señales, reglas, frenos, progresión, decisión.
app/integrations/  Garmin (lectura), Hevy (lectura y escritura), Telegram.
app/api.py       FastAPI: la API del formulario y, en su `lifespan`, los
                 trabajos del día. Un solo proceso: dos planificadores sobre la
                 misma base son dos decisiones pisándose el mismo día.
app/scheduler.py APScheduler con las horas del `config.yaml`.
static/          La PWA. Sin dependencias ni compilación.
tests/           451 tests.
```

### La idea que se repite en todo el código

Un sistema que decide solo y se equivoca en voz alta se arregla. Uno que se
equivoca en silencio, no: no llega mensaje, y no llegar mensaje se parece
demasiado a un día de descanso. Por eso hay tantos sitios donde esto se **niega
a continuar** en vez de apañárselas —una base desfasada, un `config.yaml` que
promete algo que el código no hace, un día del calendario que no declara qué
toca—. Cada uno de ellos era antes un fallo mudo.
