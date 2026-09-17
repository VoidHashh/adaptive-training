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

Implementada. Una portada y seis vistas, ocho endpoints, todos **solo de
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
| 6. Umbral de la bici | `/api/metrics/umbral` | `analysis/umbral.py` |

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

### 6. Umbral de la bici

Dos preguntas, y la segunda se calcula **sobre la respuesta de la primera**: a
partir de cuánta carga de bici baja la HRV de la mañana siguiente, y cuántos días
dura esa bajada. Por eso van en ese orden en la pantalla y las dos por encima del
pliegue, sin `plegable`: la duración de una factura que no se ha establecido que
exista no significa nada.

Es la única vista que nace de una sospecha del usuario y no de una columna: «me
suena que a partir de 150 lo noto». El trabajo del panel aquí no es confirmarla
—eso lo hace cualquier cosa— sino ponerle un número, un `n` y una corrección
alrededor para que se pueda mirar si no.

**El corte se busca, no se fija.** Los candidatos salen de los propios deciles de
sus salidas, nunca de constantes: la vista prueba cada uno, se queda con el que
más separa, y publica **cuántos ha mirado** junto con los dos vecinos del
ganador. Ese «10 miradas» es lo que convierte el número en honesto: buscar el
mejor de diez cortes y luego contar su `p` como si se hubiera elegido antes de
mirar es la forma más limpia de fabricar un hallazgo. Va a la misma tanda de
Benjamini-Hochberg que el resto de la aplicación.

**Los vecinos importan más que el número.** El corte sale con tres cifras
significativas porque es un cuantil de la muestra, y la pantalla lo redondea a
entero y dice a continuación de cuáles son los candidatos de al lado. El escalón
está *por ahí*, no exactamente ahí, y un número con coma en grande invita a la
lectura contraria.

#### La regla que enseñó esta vista: la regla mide, y también estorba

La primera versión partía las salidas en **cuartiles** y decía que el corte
estaba en 174. La segunda, en **quintiles**, decía 191. Los dos números eran
correctos y ninguno era el dato: eran dónde caía la raya de la regla que se
había usado para medir. Con deciles —los dos, el de la búsqueda y el del
dibujo— sale **150,8**, que es lo que el usuario decía.

La lección no es «usa deciles». Es que **una partición gruesa mide el ancho de
sus propias bandas** y no hay nada en el resultado que avise. Lo que sí avisa es
poner al lado los candidatos vecinos y el recuento de miradas, que es lo que se
hace ahora.

#### Escalón o cuesta: la pregunta que la vista contesta sin números

Junto al corte va **siempre** una correlación continua carga↔HRV, de la misma
tanda de corrección, y una frase que compara las dos banderas de significación:

- El corte aguanta y la continua no → **escalón**. Por debajo del corte el cuerpo
  no distingue una salida de otra; lo que cuenta es pasarlo, no cuánto.
- La continua aguanta y el corte no → **cuesta**. El «umbral» es entonces un
  sitio por donde partir, no una frontera, y publicarlo como frontera convertiría
  en hallazgo una elección de dónde cortar.
- Las dos, o ninguna, se dicen también, con lo que cada caso significa.

Esa frase **no lleva un solo número dentro, y no es estilo**: lo que compara son
dos «aguanta / no aguanta», no dos magnitudes. Meter ahí la `r` de la continua
invitaría a leerla como la fuerza del efecto cuando lo que se está diciendo es de
qué **forma** es. La `r` va al lado, con su barra, donde se compara con las demás
`r` de la aplicación.

#### El resto de la pantalla

- **Los tramos** son los cuartiles de carga con su media y su mitad central. La
  banda que queda a caballo del corte se marca y se explica: promedia salidas de
  los dos lados, así que puede leerse en contra del hallazgo sin contradecirlo.
  Está medido —la banda partida daba **+1,54** justo debajo de un corte que dice
  −7,7— y sin la explicación al lado eso es una pantalla que se desmiente sola.

  Esa marca se decide contra los bordes **ya redondeados**, los mismos que salen
  impresos. El cuartil 25 vale 43,75 por dentro y se publica como 43,8, y
  `vista_umbral` no le pasa a los tramos la frontera de dentro sino la que
  `frontera` publica, que también viene redondeada. Comparándola contra el borde
  crudo se marcaría como partida una banda cuyo `desde_carga` impreso **es** el
  corte: los dos números estarían bien por separado y se desmentirían el uno al
  otro en la misma fila, que es la peor forma de tener razón. El aviso tiene que
  ser verdad sobre la tabla **tal como está escrita**, porque esa es la única
  tabla que se ve.

  Y por debajo de `N_MINIMO_TRAMO` no sale media: sale el motivo. La media de una
  salida es esa salida otra vez, con otro nombre y con la misma pinta que una
  media de treinta. Los bordes son cuartiles de **carga**, no de cuántas salidas
  caen en cada banda, así que un tramo escuchimizado no es una esquina rara sino
  lo normal en los extremos.
- **La recuperación** solo usa salidas **aisladas** (`DIAS_AISLAMIENTO`): con otra
  salida al lado, el día +2 ya no mide la recuperación de la primera. Un día sin
  bici dentro de la cobertura es un cero de verdad y cuenta como aislamiento; un
  día sin fila es un hueco y **no** cuenta. Esa distinción es la que hace que la
  curva signifique algo.
- **Las gráficas** las dibuja `graficos.js` y no calcula ni un número: la HRV en
  el tiempo con las salidas marcadas y la raya del corte, y la curva de días +1 a
  +4 con el cero marcado.

  La franja de detrás de la línea es la **mitad central** (`BANDA`, cuartiles 25 y
  75), no el recorrido entero. Una banda que fuera del mínimo al máximo contendría
  por construcción **todas** las noches, o sea que estaría diciendo «todo lo que
  has visto entra dentro de lo normal», que es lo contrario de para lo que sirve
  una banda; y lo diría del mismo color, en el mismo sitio y solo un poco más
  ancha. Lo que se exige no son los dos percentiles sino la propiedad que los hace
  banda: **una de cada cuatro noches fuera por cada lado**.

  Y la línea suavizada solo publica un punto cuando **más de media ventana** son
  noches de verdad (`MINIMO_SUAVIZADO` sobre `SUAVIZADO`). Con menos, la media
  móvil se dibujaría igual de gruesa sobre una sola noche, y no hay forma de mirar
  el trazo y saber qué parte está medida y qué parte es una cola inventada. Esa
  regla se comprueba dos veces a propósito —la constante y el sitio donde se
  aplica—, porque un test que solo afirmara la constante seguiría verde el día que
  el dibujo se la saltara.

#### Los dos huecos que la vista dice en vez de callar

Ninguno devuelve un cero de relleno; los dos devuelven el motivo escrito.

- **Ventana más larga que el histórico**, partida en dos frases que no son la
  misma noticia. Los días de **antes** de la primera salida son historia que no
  existe y no va a existir. Los de **después** de la última son el reloj sin
  sincronizar, o sea lo único de los dos sobre lo que se puede hacer algo hoy.
  Contarlos juntos escribía «3 días de la ventana son de antes de la bici» sobre
  tres días de la semana pasada: decía lo contrario de la verdad y además escondía
  el caso arreglable.
- **La línea plana.** El dibujo escala el eje a lo medido, así que una ventana con
  todas las noches iguales no tiene alto contra el que dibujar y el navegador
  devolvería una cadena vacía. Un hueco sin motivo en mitad de la pantalla es
  justo el fallo silencioso que este panel persigue, y ningún test de los que
  comprueban que no se pinta `undefined` lo vería.

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

### El payload tiene que decir qué vista es

Por el mismo motivo, `poner()` se niega a poner encabezado a un payload que no
lleve `vista`. Dos vistas —Umbral y Percepción— habían llegado a producción sin
declararse y nadie se enteró, porque el que no dice quién es se lee igual de bien
que el que sí. La pantalla y los tests cuentan con esa etiqueta.

Lo que **no** se hace es rellenarla: `payload["vista"] = nombre` parece el arreglo
obvio y es un renombrado silencioso. `nombre` es el trozo de la URL
—`ranking-ejercicios`, con guion— y `vista` es el identificador del payload
—`ranking_ejercicios`, con subrayado—. No son el mismo dato, y escribir uno
encima del otro cambiaría la respuesta sin que nadie hubiera tocado la respuesta.
Se comprueba que esté, que es lo único que hacía falta.

Con la misma lógica, el payload de Umbral publica su `metodo`. El endpoint lo
acepta y lo valida, así que si no viajara de vuelta habría un mando en el panel
que se puede mover, que cambia el cálculo, y del que no hay forma de comprobar
desde fuera que lo haya cambiado.

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

## Líneas de trabajo abiertas

Lo que se sabe que falta, con lo que ya se sabe de ello. Va aquí y no en la
cabeza de nadie por el mismo motivo que todo lo demás de este documento: una
intención que no está escrita no se distingue de una que nunca existió.

### La carga de fuerza, que hoy no entra en ningún análisis

**Decidido el 2026-09-17: no se hace ahora.** Se retoma cuando haya unas semanas
de datos con el sistema funcionando. Lo que sigue es el estado del terreno
medido ese día, para no volver a levantarlo desde cero.

**El agujero.** El sistema mide la carga de la BICI y no mide la de la FUERZA.
`activities` tenía ese día 59 filas, del 2026-03-07 al 2026-09-12, **las 59 con
`is_cycling = 1`** y las 59 con `training_load`. Ni una de fuerza, y no es un
fallo de la integración: la fuerza no se graba en el reloj. Así que cuando la
vista de Impacto cruza "carga" contra HRV o contra las sensaciones, la palabra
carga significa bici y solo bici, y las tres sesiones de pesas de la semana
entran en el análisis como si no hubieran pasado.

**Lo que SÍ está.** El tonelaje y las series de cada sesión de fuerza, ya
calculados y ya escritos: `workout_totals` los saca del entrenamiento de Hevy y
`run_reconcile` los guarda en `workout_log.total_sets` y
`total_volume_kg` —con el calentamiento dentro, a propósito, por el escalón que
explica el docstring—. El detalle serie a serie queda en `raw_json`, así que el
desglose efectivo se recalcula sin volver a pedir nada. No hay que construir la
medida: está hecha.

**Lo que NO está es el histórico, y el motivo tiene arreglo.** Ese día
`workout_log` tenía **una sola fila** (2026-09-15, `dia_1`, 44 series,
13 815 kg). No porque falte el dato en origen, sino porque el trabajo nocturno
(`scheduler.job_reconcile`) reconcilia con `dias_atras=3`: lo anterior a que la
reconciliación empezara a correr nunca se escribió. La cuenta de Hevy sí lo tiene —14 entrenamientos y
`page_count: 2` medidos el 2026-09-13, anotados en el docstring de
`get_workouts`— y `get_workouts` pagina hacia atrás hasta el final. Es decir:
**el histórico de tonelaje es recuperable de una pasada**, con la misma forma
que el relleno de Body Battery, y esa parte no depende de esperar semanas. Lo
que sí depende de esperar es tener suficientes sesiones BAJO el sistema como
para que un coeficiente signifique algo.

**El candidato.** ACWR sobre el tonelaje —carga aguda de 7 días contra crónica
de 28— es lo que se ha hablado. Dos avisos antes de escribir una línea de
código:

- Un ACWR necesita 28 días de ventana crónica **antes** de dar su primer
  número. Con el histórico de Hevy detrás se puede tener desde el principio;
  sin él, el primer valor fiable llegaría un mes después de encenderlo.
- Y el tonelaje no es comparable entre ejercicios ni entre días de rutina: 44
  series de `dia_1` no son 44 series de `dia_3`. Sumar kilos de prensa con
  kilos de tríceps da un número que sube y baja con la rotación y no con la
  carga. Si esto llega a ser una señal, hay que decidir primero si se normaliza
  por rutina o si se compara cada rutina consigo misma.

**Lo que este documento NO dice todavía**, y es deliberado: que la carga de
fuerza vaya a ser una regla del semáforo. No lo es y no está pedido. La primera
pregunta es si el tonelaje explica algo de lo que ya se mide —que es una vista
de Impacto, no una regla— y esa respuesta llega antes y cuesta mucho menos.

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
