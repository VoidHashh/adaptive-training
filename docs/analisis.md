# Sección de métricas y análisis

Cruzar **sensaciones subjetivas** (los deslizadores del check-in) con **datos
objetivos** (Garmin y Hevy). No es replicar Garmin Connect ni Hevy.

Lo que NO va aquí: gráficas de sueño, de HRV o el detalle de una salida. Eso ya
existe en Garmin Connect y hacerlo otra vez peor no ayuda a nadie.

> **Este documento describía una sección pendiente y se quedó descrito así
> después de construirla.** Decía "no implementada todavía" con cinco vistas ya
> en producción, y mandaba los endpoints a un prefijo `analysis` que no existe
> ni ha existido nunca: el bueno es `metrics`. (La ruta equivocada no se escribe
> aquí entera a propósito: `tests/test_docs.py` prohíbe que este fichero nombre
> ninguna ruta que no exista, y una ruta falsa citada como ejemplo se copia
> igual de bien que una recomendada.) Se deja escrito porque es el
> mismo fallo que este proyecto lleva meses cazando en el código -el validador
> que certificaba una sección muerta, la fila de `workout_log` que decía "Sí" y
> no era verdad- y no hay motivo para que la documentación esté exenta. Un
> documento de diseño que no se actualiza al implementar no envejece hacia
> "incompleto": envejece hacia "falso", que es peor, porque se sigue leyendo
> con la misma confianza.

## Estado

Implementada. Cinco vistas, seis endpoints, todos **solo de lectura** bajo
`/api/metrics/`:

| Vista | Endpoint | Módulo |
|-------|----------|--------|
| 1. Concordancia | `/api/metrics/concordancia` | `analysis/concordancia.py` |
| 2. Desfase | `/api/metrics/desfase` | `analysis/concordancia.py` |
| 3. Impacto | `/api/metrics/impacto` | `analysis/impacto.py` |
| 3b. Ranking de ejercicios | `/api/metrics/ranking-ejercicios` | `analysis/impacto.py` |
| 4. Auditoría | `/api/metrics/auditoria` | `analysis/auditoria.py` |
| 5. Percepción | `/api/metrics/percepcion` | `analysis/rendimiento.py` |

El front es `static/metricas.html` + `static/metricas.js`, que no escribe a mano
ni un deslizador ni una regla: todo lo que se puede elegir se rellena de lo que
manda el servidor. Escrito a mano, añadir una señal al `config.yaml` la dejaría
fuera de las métricas sin un solo error.

## Las vistas

### 1. Concordancia
Deslizadores frente a métricas de Garmin del mismo día, normalizados a una
escala común. ¿La percepción de cansancio coincide con HRV y pulso en reposo?
¿La de sueño coincide con lo que mide el reloj?

### 2. Desfase
Correlación con retardo **de −3 a +3 días**, en los dos sentidos. La pregunta es
si la percepción se adelanta o se retrasa respecto al reloj. Si se adelanta de
forma consistente, eso cambia cuánto debería pesar el check-in en el semáforo.

### 3. Impacto del entrenamiento
Cruce entre lo entrenado (Hevy: volumen, ejercicios concretos; Garmin: salidas y
su clasificación) y el estado de los 1-3 días siguientes. Dos preguntas
concretas:
- ¿La molestia lumbar sube después del Día 2?
- ¿Cuántos días de HRV cuesta una salida INTENSA?

El **ranking de ejercicios** es la misma pregunta bajada al ejercicio concreto,
y por eso comparte módulo: qué levanta la molestia y qué no.

### 4. Auditoría del semáforo
Histórico de colores por día con los deslizadores superpuestos, y un contador de
cuántas veces disparó cada regla. Es la vista para recalibrar umbrales.

### 5. Percepción
Lo que la mañana prometía frente a lo que de verdad salió: la decisión del motor
contra el resultado registrado después. Es la única vista que mira hacia
adelante desde la decisión en vez de hacia atrás desde el dato.

## El aviso metodológico, en la interfaz

Con menos de 6-8 semanas de datos las correlaciones son ruido. La sección
**muestra el número de días disponibles** y avisa cuando la muestra es
insuficiente, en vez de pintar un coeficiente sin contexto. Un r=0.8 sobre nueve
días invita a recalibrar el semáforo con ruido, y esa es una decisión peor que
no tener la vista.

Es la misma disciplina que `sin_muestra` en la capa de tendencia y que `skipped`
frente a `not_fired` en `rules.py`: no poder calcular algo es un resultado, y se
dice. Callarlo convierte "no lo he mirado" en "no pasa nada".

## Qué hace falta guardar (y qué NO se estaba guardando)

| Vista | De dónde sale | ¿Está? |
|-------|---------------|--------|
| 1, 2, 5 | `checkins` (una fila por día, nulo = sin contestar) | Sí |
| 1, 2, 5 | `daily_metrics` (`hrv`, `rhr`, `sleep_min`, `sleep_score`, `body_battery`) | Sí, desde que `run_daily` la escribe |
| 3, 3b | `workout_log` (volumen, series) + `raw_json` para el detalle por ejercicio | Sí, desde que `run_reconcile` los rellena |
| 3 | `activities` (`intensity_level`, `training_load`, zonas) | Sí, desde que `run_daily` la escribe |
| 4, 5 | `decisions.fired_rules_json` y `skipped_rules_json`, append-only con `is_current` | Sí |

`daily_metrics` tuvo además tres columnas que ya no están -`readiness`,
`load_3d` y `load_7d`-, cada una por un motivo distinto y los tres escritos en
el docstring del modelo. Resumen: `readiness` era imposible de llenar con este
reloj, y las otras dos eran una copia de un cálculo que nadie leía.

Las dos tablas del medio llevaban desde el primer día **declaradas en
`models.py` y sin que nadie las escribiera**. No daba ningún error: el sistema
leía las métricas de Garmin cada mañana, decidía con ellas y las tiraba.

- De `daily_metrics` sobrevivía una copia parcial dentro de
  `decisions.inputs_snapshot_json`: solo de los días en que hubo decisión, y
  solo de las señales que el motor evalúa.
- De `activities`, ni eso. El crudo sí está a salvo en
  `data/cache/activities.json` -esa caché se fusiona y nunca se poda-, pero la
  **clasificación** no: `Signals.rides` está fuera de `values` a propósito, así
  que el `suave/media/intensa` no entraba en el snapshot. Y no se puede
  reconstruir después, porque depende de los umbrales del `config.yaml` del día
  en que se hizo.

Ya se escriben las dos, en `run_daily`, con dos reglas:

- **Un `None` nuevo no pisa un dato viejo.** La ventana se relee cada mañana; si
  la segunda pasada trae `hrv=None` por un 429, copiarlo encima borraría el dato
  bueno y dejaría una fila con un hueco indistinguible de una noche sin reloj.
- **Si archivar falla, la mañana termina igual** y el fallo viaja en
  `problemas`. Perder un día de histórico es malo; quedarse sin plan por no
  poder guardar una fila, peor.

### La fila de `workout_log` decía "Sí" y no era verdad

Esta tabla llevaba el mismo problema que las otras dos, pero además **estaba
certificada como resuelta en esta misma tabla**: la vista 3 declaraba salir de
`workout_log (volumen, series) + raw_json`, y de esas cuatro columnas
(`duration_s`, `total_sets`, `total_volume_kg`, `raw_json`) no se escribía
NINGUNA. `run_reconcile` guardaba el id, el día, el título y `all_sets_at_target`,
y tiraba el entrenamiento entero después de mirarlo.

Es el mismo patrón que el validador que daba por buena una sección muerta: el
documento que tenía que avisar del hueco era justo el que decía que no lo había.
Ahora se rellenan las cuatro. Dos consecuencias que conviene saber:

- `total_sets` y `total_volume_kg` incluyen el calentamiento. Excluirlo daría un
  escalón en la gráfica el día que se desplegó el marcado de series de
  calentamiento en Hevy, y ese escalón no significaría nada. El desglose
  efectivo se recalcula desde `raw_json`.
- `raw_json` es el ÚNICO sitio donde queda el peso y las reps realmente
  levantados en cada serie. Ni la decisión ni el estado del motor los guardan:
  solo guardan si se alcanzó el objetivo. Sin esta columna, "¿cuánto subió el
  hip thrust en tres meses?" no tiene respuesta posible.

### Wellness: se podan las series por minuto, no los datos

`daily_metrics.raw_json` guarda las cinco respuestas de Garmin de las que salen
los números. No caben tal cual: sueño y body battery traen la serie por minutos
de la noche entera, cientos de KB al día. Se poda **por forma y no por nombre**
(`repository.podar_crudo`): toda lista de más de 50 elementos se sustituye por
una muestra de dos y un recuento. Una lista de campos conocidos dejaría de
reconocer el día que Garmin renombre uno, y no fallaría: engordaría la base en
silencio. Los escalares —fases de sueño, respiración, mínimos— pasan enteros, y
es ahí donde vive lo que haría falta más adelante.

## Notas de diseño

- Los endpoints van bajo `/api/metrics/...` y son **solo de lectura**. El
  service worker no puede cachearlos, por lo mismo que el resto de `/api/`.
- Los coeficientes se calculan en el servidor, no en el móvil: es donde están
  los datos y donde se puede probar con tests.
- Toda respuesta lleva `n` y la ventana al lado del coeficiente. Sin muestra no
  se devuelve el número: se devuelve **por qué** no se devuelve.
- Un deslizador sin contestar es `NULL`, no un 5. Los pares con nulo se excluyen
  del cálculo y eso reduce la n, que es exactamente lo que debe pasar.
- Las dos poblaciones de cualquier comparación se construyen **disjuntas**
  (`_previas` filtra `date < antes_de`, `contraste` parte en `con`/`sin`). Una
  ventana corta metida dentro de su propia referencia se compara en parte
  consigo misma y amortigua la señal; pasó en `engine/tendencia.py` y está
  documentado allí con la medida.
