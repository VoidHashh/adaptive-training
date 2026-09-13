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

Implementada. Una portada y cinco vistas, siete endpoints, todos **solo de
lectura** bajo `/api/metrics/`:

| Vista | Endpoint | Módulo |
|-------|----------|--------|
| 0. Portada | `/api/metrics/portada` | `analysis/portada.py` |
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

### 0. Portada

Lo que YA SE SABE, antes de pedirle a nadie que elija nada. Existe porque las
cinco vistas de abajo empezaban por el aparato de medir -un desplegable de
variables y una tabla de coeficientes- en vez de por la medida. Medido el
2026-09-13: había 27 relaciones fiables calculadas, serializadas y enviadas al
navegador, y la pantalla de entrada no enseñaba ninguna.

Tres bloques: **cómo voy** (la última semana contra el propio histórico, nunca
contra constantes), **qué ha cambiado** (esta semana contra la anterior) y **lo
que ya sé de ti**, que es un hallazgo por decisión y no por variable.

Lo de "por decisión" no es presentación. `minutos_bici` y `desnivel_bici`
correlacionan **a 0,9986** entre ellas en este histórico: no son dos medidas
parecidas, son la misma columna en otras unidades. Enseñarlas como dos hallazgos
sugiere dos pruebas independientes donde hay una, y eso no es repetitivo, es una
exageración de la evidencia. La agrupación se declara en `metrics.grupos_exposicion`
del `config.yaml`, y una exposición que no caiga en ningún grupo **revienta**
(`portada.grupo_de`) en vez de desaparecer sin más.

Del resto del grupo, las que apuntan al mismo lado van en `confirmada_por` -"la
misma cosa medida de otra manera"- y las que apuntan al contrario van en
`discrepa`, dichas y sin resolver. Esa separación **se comprueba por el signo**,
y no es hipotética: la primera versión citaba «las salidas medias» como
confirmación de que las intensas bajan la HRV, cuando su casilla más fuerte es un
**+0,25** del signo opuesto. Una frase falsa en la pantalla de entrada, montada
con números todos correctos y sin un solo error por ningún lado.

La mitad de abajo, **"lo que todavía no puedo saber"**, tiene el mismo rango que
la de arriba y se calcula contando filas. Una vista vacía que no explica su vacío
es un fallo silencioso de interfaz: quien la abre no distingue "aquí no pasa
nada" de "aquí falta un dato que nadie trae".

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

## El encabezado de cada vista (`analysis/encabezados.py`)

Cada endpoint de `/api/metrics/` -menos la portada, que es un encabezado de
arriba abajo- devuelve un bloque `encabezado` con cuatro cosas: la **pregunta**
que contesta esa vista, su **estado** (`vacio` / `parcial` / `con_datos`), un
**resumen** en castellano y los **números crudos** `n` y `de` al lado, para que
el cliente pueda pintar una barra sin parsear la frase.

**Nada de eso está escrito a mano salvo la pregunta.** La pregunta no cambia
mientras la vista sea la misma; todo lo demás sale de contar lo que la vista
acaba de devolver. Es el mismo motivo por el que este documento tiene un test
que lo lee: un estado escrito a mano no envejece hacia "viejo", envejece hacia
"falso", y se sigue leyendo con la misma confianza.

**Cada vista se cuenta en su propia moneda.** No hay un `n` universal y no se
finge que lo haya: en Concordancia la unidad es el par de series correlacionado,
en Auditoría el día con decisión, en Percepción la sesión que se ha podido
juzgar. En Impacto se cuentan **casillas, no filas** -una fila con uno de sus
tres retardos calculado no está calculada, está calculada un tercio-, y en el
ranking se cuentan los ejercicios **ordenados**, no los listados: los que no
tienen el retardo de ordenación salen al final sin esconderse, pero no están
rankeados.

Que exista `parcial` es el punto. Sin él, una vista con dos pares de catorce se
pintaría igual que una con los catorce.

Junto a lo que la vista ha podido **mirar**, Impacto añade `fiables`: cuántas de
las calculadas aguantan la corrección por comparaciones múltiples. Van los dos
números porque dicen cosas distintas, y enseñar solo el segundo haría que una
vista incapaz de calcular nada se leyera igual que una que ha calculado 135 y no
ha encontrado nada. Auditoría hace lo mismo con `reglas_sin_estrenar`: una regla
que no ha disparado nunca no está rota, pero tampoco probada.

### La clave ausente es un error, la lista vacía no

`v.get("pares") or []` devuelve el mismo `[]` en dos situaciones que no se
parecen en nada: una vista sin datos todavía, y una vista a la que le han
renombrado la clave. La primera es el lunes por la mañana de un sistema que
arranca; la segunda es un encabezado que dirá "Todavía no" para siempre, con
toda la seguridad del mundo, mientras la vista debajo pinta sus 35 filas. Así
que la ausencia de la clave **revienta con un mensaje que explica qué pasó**, y
la lista vacía no.

El registro `POR_VISTA` se comprueba contra la **tabla de rutas de la
aplicación**, no contra una lista escrita al lado: una vista nueva sin
encabezado rompe el test el día que se escribe, y un encabezado cuya vista ya no
existe también.

### El desplegable de Impacto ya no se estrena vacío

Impacto ofrece doce respuestas y el cliente abría en la primera de la rejilla,
que es el cansancio. Con el sistema recién arrancado el cansancio tiene **0 de
33** casillas, así que la vista se estrenaba vacía teniendo 135 calculadas a dos
clics. Ahora el payload trae `respuestas` -cada opción con su `n`, su `de` y un
`vacia` booleano- y `respuesta_por_defecto`, que es la primera que tenga algo
dentro. Si **ninguna** tiene datos, `respuesta_por_defecto` es `null` en vez de
la primera: que no haya con qué abrir es un estado real y decirlo deja enseñar
el vacío explicado en vez de un desplegable que promete doce vistas y abre en
una muerta.

El recuento va en el **servidor** y no en el JS a propósito. Si el cliente
recorriera la rejilla para averiguar qué puede enseñar habría dos sitios
contando lo mismo, y el día que discrepen ganaría el que no se puede probar.

## El aviso metodológico, en la interfaz

Con menos de 6-8 semanas de datos las correlaciones son ruido. La sección
**muestra el número de días disponibles** y avisa cuando la muestra es
insuficiente, en vez de pintar un coeficiente sin contexto. Un r=0.8 sobre nueve
días invita a recalibrar el semáforo con ruido, y esa es una decisión peor que
no tener la vista.

Es la misma disciplina que `sin_muestra` en la capa de tendencia y que `skipped`
frente a `not_fired` en `rules.py`: no poder calcular algo es un resultado, y se
dice. Callarlo convierte "no lo he mirado" en "no pasa nada".

## Reglas de método

Esto no son notas de implementación: son errores cometidos al ANALIZAR, que no
dan ningún fallo y no los caza ningún test, así que el único sitio donde pueden
vivir es aquí.

### Los títulos de Hevy no son dato

Está medido y escrito en `routine_key_de`: de los 14 entrenamientos reales de la
cuenta, **cuatro llevan un título que nombra una rutina distinta de la que dice
su `routine_id`**. La clasificación ya sale del id por eso.

Lo que faltaba decir es que **la regla vale también para inferir intención**. El
2026-09-13, en el mismo análisis en que se había establecido ese 29% de títulos
mentirosos, se usaron dos entrenamientos titulados «Día 2 y 3 HIIT» como prueba
de que el usuario quería HIIT en el Día 3, y se propuso cambiarle el
`config.yaml`. Eran de agosto, de un ciclo terminado, y lo que hace ahora es
«Día 1 HIIT» y «Día 2 HIIT» -justo lo que el config ya decía-. El fallo no fue
de lectura: fue usar como evidencia una cadena de texto que ya se sabía poco
fiable, y usarla además para proponer un cambio de comportamiento del motor.

Lo que se HIZO se lee del `routine_id` y de los ejercicios. Lo que se QUIERE
hacer se lee del `config.yaml`, o se pregunta. El título no sirve para ninguna
de las dos cosas.

### El histórico viejo no describe la práctica actual

Corolario del anterior y con la misma víctima. Un ciclo de cuatro semanas que
terminó hace un mes está en la base de datos exactamente igual de presente que
lo de esta semana, y una consulta sin ventana los mezcla sin avisar. Antes de
concluir "esto es lo que hace", hay que mirar CUÁNDO lo hacía. La ventana no es
un adorno del `WHERE`: es la diferencia entre describir una práctica y describir
un recuerdo.

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
