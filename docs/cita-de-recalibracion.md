# Cita de recalibración

Escrito el **18 de septiembre de 2026**, con el sistema ya decidiendo de verdad
(`dry_run: false`, escritura en Hevy y Telegram activos) y a punto de dejarlo
correr unas semanas sin tocarlo.

Este documento existe para no depender de la memoria. Dice tres cosas: en qué
estado quedó, qué hay que mirar y cuándo, y qué es normal aunque parezca roto.

> Las fechas de las citas son **estimaciones con el ritmo de hoy** (un check-in
> al día). Si se salta días, se retrasan; el número que manda no es la fecha,
> es el recuento. Cada cita dice cómo comprobar el recuento de verdad en vez de
> fiarse del calendario.

---

## 1. De dónde se parte

Contado sobre la base del contenedor el 18 de septiembre de 2026:

| Qué | Cuánto | Desde | Hasta |
|---|---|---|---|
| Check-ins | **3** | 2026-09-15 | 2026-09-18 |
| Decisiones guardadas | 5 | 2026-09-14 | 2026-09-18 |
| Mensajes escritos en Hevy | 5 | 2026-09-14 | 2026-09-18 |
| Previsualizaciones | **1** | 2026-09-18 | 2026-09-18 |
| Desacuerdos marcados | **0** | — | — |
| Bienestar de Garmin | 188 días | 2026-03-15 | 2026-09-18 |
| Salidas de bici | 59 | 2026-03-07 | 2026-09-12 |
| Entrenos de fuerza (Hevy) | 16 | 2026-08-14 | 2026-09-16 |

**El cuello de botella es el check-in, y con diferencia.** Garmin trae seis
meses y Hevy cinco semanas; el check-in lleva cuatro días. Casi todo lo que
está en blanco lo está por eso, y se desatasca solo contestando el formulario
cada mañana. No hay nada que arreglar para que esas vistas empiecen a hablar.

---

## 2. Los umbrales, que son los que mandan

No son opiniones repartidas por el código: son constantes con nombre, y la
pantalla los cita cuando no llega.

| Constante | Vale | Dónde | Qué desbloquea |
|---|---|---|---|
| `N_MINIMO_CALCULABLE` | 3 | `app/analysis/stats.py:56` | que una relación se pueda calcular |
| `N_MINIMO_FIABLE` | 20 | `app/analysis/stats.py:61` | que además se pueda creer |
| `N_MINIMO_EXPUESTOS` | 3 | `app/analysis/impacto.py:69` | comparar «con» contra «sin» (3 de cada) |
| `MINIMO_JUICIOS` | 5 | `app/analysis/calibracion.py:83` | el veredicto de calibración |
| `MINIMO_RECIENTES` | 4 | `app/analysis/portada.py:69` | la media de 7 días de la portada |
| `MINIMO_REFERENCIA` | 20 | `app/analysis/portada.py:74` | la referencia contra la que se compara |
| `MINIMO_SLIDERS` | 4 | `app/analysis/rendimiento.py:90` | que una sesión se pueda juzgar (4 de 6) |
| `N_MINIMO_TRAMO` | 3 | `app/analysis/umbral.py:141` | la media de un tramo de carga |

---

## 3. Las citas

### Cita 1 — hacia el **20 de septiembre** (2 check-ins más)

Con tres días que tengan a la vez check-in y el otro dato, **Coincide** y
**Retraso** dejan de decir «solo hay 2 días con las dos cosas medidas» y
empiezan a dibujar. Serán números malos —tres puntos no son nada— y la propia
pantalla lo dirá.

- **Qué revisar:** que las vistas Coincide, Retraso y la portada pasen de «no
  hay bastante» a un número. Si con 4-5 check-ins seguidos alguna sigue
  diciendo que solo hay 2 días, **eso sí es un fallo**: significa que el
  check-in se está guardando y no se está emparejando.
- **Cómo:** abrir `http://192.168.8.201:8317/metricas.html#concordancia` y
  `#desfase`.

### Cita 2 — hacia el **28 de septiembre** (dos semanas de plan)

Coincide con la revisión del plan de las semanas 49-50, que ya estaba puesta.

- **Qué revisar:** la vista **Real** (auditoría). Hoy dice «no hay decisión
  guardada para este día» en casi todos porque las decisiones empezaron el 14.
  Para entonces habrá dos semanas seguidas y se podrá ver, por primera vez, qué
  reglas han disparado y cuáles no.
- **Ojo con una cosa:** la tabla `rule_states` está **a cero**. Mientras siga
  así, la parte de la auditoría que cuenta la vida de cada regla no tendrá nada
  que contar. Si el 28 sigue vacía con dos semanas de decisiones detrás, hay
  que mirar por qué: es de las pocas cosas de esta lista que no se explica sola
  por falta de días.
- **Cómo:** `#auditoria`, y
  `docker exec adaptive-training python -c "import sqlite3; print(sqlite3.connect('/app/data/app.db').execute('select count(*) from rule_states').fetchone())"`

### Cita 3 — hacia el **8 de octubre** (20 check-ins)

`N_MINIMO_FIABLE`. Es el primer día en que un número de estas pantallas se
puede leer como algo más que un indicio.

- **Qué revisar:** Coincide y Efecto (impacto) con veinte días. Aquí es donde
  empieza a tener sentido preguntarse si algún deslizador no aporta nada y
  sobra del formulario.
- **Cómo:** `#concordancia`, `#impacto`, y la vista Umbral si ha habido bici.

### Cita 4 — hacia el **10 de noviembre** (8 semanas desde el primer check-in)

El plazo que el propio proyecto se puso antes de fiarse de una correlación
—está escrito en `docs/analisis.md` y en el aviso de muestra insuficiente de la
interfaz—. Ocho semanas desde el 15 de septiembre.

- **Qué revisar:** todo lo que hasta entonces se haya leído como «indicio» pasa
  a poder discutirse. Es la cita para decidir si alguna regla del `config.yaml`
  se cambia.

### Cita sin fecha — **calibración**

No depende del calendario sino de cuántas veces se pulse **Previsualizar** y se
marque si se está de acuerdo. Hoy: 1 previsualización, **0 opiniones**.

Para el veredicto hacen falta **5 juicios**, y un juicio no es solo un
desacuerdo: hace falta haber marcado el desacuerdo, **haber pedido otra
sesión**, que se ejecutara la que se pidió, y que esa sesión se pudiera
puntuar. Es deliberadamente exigente, y el motivo está en `docs/analisis.md`:
el juez no es independiente, porque `comp_rpe` va dentro de `performance_pct`.

- **Cómo ver cuánto falta:** `#calibracion` lo dice con todas las letras («van
  N de los 5»).
- **Lo que hace falta para que avance:** pulsar Previsualizar por la mañana
  antes de enviar, y marcar «no estoy de acuerdo» cuando de verdad no se esté.
  Sin eso esta vista no se mueve nunca, por muchos meses que pasen.

---

## 4. Qué es normal estas semanas aunque parezca roto

- **Vistas en blanco diciendo «no hay bastante».** Es lo correcto y es la mitad
  del diseño: la pantalla dice cuánto falta en vez de pintar un cero. «Solo hay
  2 días con las dos cosas medidas; falta 1» no es un error, es el contador.
- **Percepción sin contador.** Dice «todavía no hay ninguna sesión que se haya
  podido juzgar». Las 16 sesiones de Hevy son anteriores al check-in, así que
  tienen 0 de 6 deslizadores contestados y no llegan a `MINIMO_SLIDERS = 4`. Se
  arregla solo según se vayan acumulando sesiones con su check-in delante.
- **Reglas que no disparan.** Una regla que no dispara no está rota: está
  diciendo que no se ha dado su condición. Lo que sí habría que mirar es una
  que dispare TODOS los días.
- **Ámbar por precaución.** El mensaje del 18 de septiembre decía «Ámbar por
  precaución: no se han podido evaluar...». Con pocos datos es el
  comportamiento previsto —ante la duda, no subir carga—, y con una hernia L4-L5
  es la dirección correcta del error.
- **La calibración parada en cero.** No avanza con el tiempo, solo con el uso
  del botón.
- **El botón de previsualizar tarda un arranque en aparecer.** Desde la v19 el
  service worker sirve el armazón del caché de su versión: tras reconstruir, el
  móvil instala la versión nueva en un arranque y la usa en el siguiente.

---

## 5. Qué NO es normal

Estas son las señales de que algo va mal de verdad:

Desde el 18 de septiembre hay un **trabajo de vigilancia a las 09:45** que
mira el estado del sistema y manda un Telegram SOLO si hay algo: una escritura
de Hevy a medias, una credencial que falta, el `config.yaml` del disco distinto
del cargado o el reloj desajustado. Avisa cada mañana mientras dure, a
propósito. Lo que NO puede vigilar es a sí mismo: si el planificador no
arranca, este trabajo tampoco, y de eso avisan `auditar_arranque` y el
healthcheck de Docker.

| Señal | Qué significa |
|---|---|
| No llega el Telegram diario | El planificador no corrió, o falló el envío. Mirar `/api/health`. |
| Llega un ⚠️ de trabajo fallido o no ejecutado | Lo manda el propio sistema. Dice qué trabajo y por qué. |
| La pantalla de check-in avisa de escritura a medias | Una rutina de Hevy quedó escrita a medias. Ver §6. |
| Con 5+ check-ins seguidos, Coincide sigue en «solo hay 2 días» | El check-in se guarda y no se empareja. |
| `rule_states` sigue a 0 en la cita 2 | La auditoría de reglas no está registrando nada. |
| `/api/health` con `in_sync: false` | El `config.yaml` del disco no es el que está decidiendo. |
| `/api/health` con `pending_write` no nulo | Escritura de Hevy a medias. |

Comprobación rápida de una sola línea:

```bash
curl -s http://192.168.8.201:8317/api/health | python -m json.tool
```

Hoy sale: `status: ok`, `secrets_missing: []`, `problemas: []`,
`scheduler.running: true`, `clock.matches: true`, `config_file.in_sync: true`,
`writes.pending_write: null`.

---

## 6. Comandos

Todos contra el contenedor, que es donde están los datos de verdad (el `data/`
del repositorio es una base de desarrollo vieja y **no** es la que corre).

```bash
docker exec adaptive-training python -m app.rutina estado
```

Escrituras de Hevy a medias y copias por rutina. Es lo primero que hay que
mirar si la pantalla avisa de una escritura a medias. `revertir` deshace;
`cerrar` retira la marca cuando Hevy ya coincide con la copia.

```bash
docker exec adaptive-training python -m app.cli --dry-run --date 2026-09-25
```

Decide un día y enseña el resultado **sin escribir en Hevy ni enviar Telegram**.
Es la forma de preguntarle al motor «¿qué habrías hecho?» sin consecuencias.

```bash
docker exec adaptive-training python -m app.cli --dry-run --checkin lower=6,fatigue=8
```

Lo mismo con un check-in inventado: para probar si una regla dispara cuando
debería, sin esperar a tener un mal día de verdad.

```bash
docker logs --since 24h adaptive-training | grep -iE "error|warning"
```

---

## 7. Lo que quedó construido y esperando

Nada de esto está a medias: está terminado y sin datos que enseñar.

- **Las ocho vistas de métricas.** Todas calculan; casi todas dicen cuánto
  falta.
- **Calibración** (`/api/metrics/calibracion`). La única que depende de un
  hábito y no del tiempo.
- **Previsualizar**, con su tarjeta y su desacuerdo. Recién visible: hasta el
  commit `518fb3e` el botón se pintaba sin una sola regla de CSS y medía
  92 × 20 píxeles en gris sobre fondo oscuro.

## 7-bis. El cambio de contrato del 25/09/2026

**El sistema aconseja, no decide.** Decisión del usuario, y cambia cómo se
mueven las cargas:

- **Una serie más ligera ya no bloquea una subida.** Si la sesión sube en rampa
  —30, 40, 50 con las reps completas— se adopta. Antes la serie de 30
  invalidaba los 50. Lo que sigue bloqueando es que las **reps** se queden
  cortas: 70 kg a 4 reps cuando se pedían 10 es un peso intentado, no
  levantado. Y eso vale para TODAS las series que se adoptan, también la
  tercera de un día en que el plan pedía dos.
- **Se adopta la forma, no solo el tope.** Hiciste 30/40/50, la próxima vez
  30/40/50. Hasta la tarde del 25/09 se movían todas las series lo que había
  subido la más pesada, y un objetivo plano hecho en rampa quedaba con todas
  las series al tope: la aducción hecha a 50/60/80 quedó en 55/70/80, la prensa
  a una pierna hecha a 40/50/60 en 60/60/60. Se copian los pesos; las reps
  siguen siendo las del objetivo.
- **Bajar sigue igual de lento**: 3 sesiones seguidas por debajo, y se baja a
  la mejor de las tres —ahora con su forma entera—. Ahí el peso sí cuenta.
- **El tope de salto pasa de 5 kg / 20% a 30 kg / 60%.** En seis meses había
  actuado cuatro veces y las cuatro eran levantamientos reales, ninguna una
  errata: frenaba la realidad, no el ruido. Sigue frenando un dígito de más.

- **La última serie corta de reps ni suma ni borra la racha.** Con tus
  palabras: el 12/12/9 «es válida, pero no cuenta para el próximo día, que
  seguiría siendo 12/12/12». No paga ninguna subida, y tampoco obliga a la
  cadena posterior -que pide dos limpias seguidas- a empezar de cero. Solo ese
  caso: una serie de en medio corta, una de menos o un peso por debajo siguen
  rompiendo la racha.
- **«Hoy» abre con la decisión del día** cuando ya está decidido, con el
  formulario plegado detrás de «Cambiar mis respuestas». La tarjeta dice Hevy y
  Telegram en palabras. Si al abrir se recalcula el día, la tarjeta se repinta.
- **La puerta de subir mira cada ejercicio, no la rutina entera**
  (`progression.gate.compliance_scope: exercise`). Con la rutina entera, un
  ejercicio incompleto bastaba para que no subiera nada, y en las diez primeras
  decisiones la puerta no se abrió ni una vez. Ahora cada ejercicio sube -reps
  o carga- si SU última sesión estuvo completa; el semáforo verde, los frenos
  lumbares y la descarga siguen siendo de la rutina entera. **Qué vigilar:**
  que suban cosas donde antes no subía nada es lo esperado; si algo sube que no
  reconoces, el sitio es `clean_sessions_required` de ese ejercicio.
- **Rachas y pesos se mueven al cerrar el día, no al llegar cada entreno.**
  Llegar es solo apuntarse; el día se cierra cuando ha terminado -a la mañana
  siguiente, antes de decidir- con todos sus entrenos, el formulario de después
  y la versión de Hevy de ese momento. Así cuenta «lo hice y no lo apunté»,
  una sesión partida en dos se evalúa entera, y un peso corregido en Hevy esa
  misma tarde entra. Lo corregido DESPUÉS del cierre no entra, y un entreno que
  llega con el día ya cerrado se apunta sin mover nada.
- **El HIIT cuenta por lo que se hizo, no por la rutina.** Al fusionar el HIIT
  del Día 2 en el Día 2, el recuento de sesiones intensas dejó de ver sus
  intervalos, y también los «Día 2 HIIT» ya hechos. Ahora un entreno cuenta
  como HIIT si sale de un bloque de `hiit.blocks` o si lleva alguno de los
  intervalos de `hiit.embedded`. Eso recupera también el del 16/09, hecho
  dentro del Día 2 sin que el plan lo pidiera, que tampoco contaba antes.

**Qué vigilar en la cita:** que las cargas no se disparen. El freno que queda
arriba es solo el de las reps y el tope ancho; si en dos semanas hay subidas que
no reconoces, el sitio donde apretar es `down_after_sessions` o el tope, no la
guarda que se acaba de quitar.

**Lo que NO hace este cambio: reescribir el pasado.** Las cuatro adopciones que
se rechazaron en septiembre siguen rechazadas, y sus objetivos siguen donde
estaban. La patada atrás sigue en 35 kg aunque se levantaran 50 el día 21. Se
arregla de dos maneras: volver a hacer el ejercicio —la próxima reconciliación
ya lo adopta— o fijarlo a mano con `scripts/fijar_carga.py`.

**Lo que SÍ se reescribió, a mano, la tarde del 25/09.** Las diez cargas que el
fallo de la forma había dejado por encima de lo levantado se fijaron con
`fijar_carga.py --contenedor` a la forma que se hizo, que es lo que el código
nuevo habría guardado. En las diez, la carga no se había movido desde esa
adopción. Antes → después:

| Rutina / ejercicio | Antes | Después (lo hecho) |
|---|---|---|
| dia_1 prensa_horizontal | 90/100/120 | 70/90/120 |
| dia_2 peso_muerto_smith | 25/27,5/27,5 | 20/25/27,5 |
| dia_2 jalon_al_pecho | 45/45/45 | 40/42,5/45 |
| dia_2 face_pull | 15/15/15 | 12,5/12,5/15 |
| dia_3 aduccion_cadera | 55/70/80 | 50/60/80 |
| dia_3 contractora_pecho | 27,5/30/32,5 | 25/27,5/32,5 |
| dia_3 curl_predicador | 16,25/18,75/20 | 15/17,5/20 |
| dia_3 extension_triceps_polea | 15/17,5/20 | 12,5/15/20 |
| dia_3 prensa_una_pierna | 60/60/60 | 40/50/60 |
| dia_3 jalon_brazos_rectos | 15/15/15 | 12,5/12,5/15 |

La serie top no cambia en ninguna: lo que baja son las de delante, que estaban
por encima de lo que se levantó.

## 8. Lo que queda sin hacer, anotado

- **La clave `trend.nivel` del `config.yaml`, DESPUÉS de reconstruir.** El
  detector de nivel (24/09/2026) viaja en el código ya, pero **callado**: sin
  su sección de configuración no hace nada. No se puede añadir la clave antes
  de la reconstrucción porque el `config.yaml` va bind-mounted y lo lee el
  contenedor **en marcha**, cuyo validador todavía no la conoce y la rechaza.
  El orden es: reconstruir primero, añadir la clave después. Lo que hay que
  pegar en la sección `trend`:

  ```yaml
    nivel:
      historico_dias: 180
      reciente_dias: 30
      percentil_max: 12
      dias_min: 4
  ```

  Con eso hay que hacer **dos cosas más**, y la segunda es la que impide que
  esto se quede a medias para siempre:

  1. Cambiar una línea de `tests/test_runner.py`: el test que afirma que la
     ventana de wellness son 90 días pasa a ser 187, porque el detector
     necesita tener delante los 180 de referencia más los 7 de la línea base
     del más antiguo. El test lo dirá con ese número en el mensaje de fallo.
  2. **Añadir a `tests/test_config_loader.py` un test que exija que la clave
     esté**, y borrar el párrafo de `app/engine/tendencia.py` que explica por
     qué todavía no lo hay. Hoy la sección es opcional de verdad: si nadie la
     añade, el detector no habla nunca y **ningún test se pone rojo**. Ese es
     exactamente el defecto que este repositorio persigue —una pieza que
     certifica que no falta nada mientras falta— y de momento lo único que lo
     sujeta es este apunte.

  **Qué empieza a decir.** Una línea en el mensaje de la mañana cuando la
  línea base de HRV lleve 4 días o más en el 12% más bajo de los 150 días
  **anteriores a los 30 últimos** —la referencia excluye el tramo que juzga,
  que es lo que impide que el umbral persiga a la señal hacia abajo—. Sobre el
  histórico hablaría el 3,6% de los días, en un solo episodio. No decide nada:
  no toca el semáforo, ni la sesión, ni las cargas.

  **Por qué existe.** `hrv_ratio` divide el HRV de hoy entre la media de los 7
  días anteriores, así que una bajada lenta se le escapa: el denominador baja
  con el numerador. Medido del 10 al 24 de septiembre de 2026, el HRV pasó de
  ~51 a 37, Garmin marcó su veredicto como bajo nueve días seguidos —la racha
  más larga del registro— y `hrv_ratio` no bajó de 0,86 ni un día; el 21 y el
  22 marcó 1,005 y 1,009, o sea «normal». El 22 el semáforo salió **verde**.
  Los tres detectores de tendencia que ya había leen colores, y los colores no
  se movieron, así que también se callaron.

- **El control de tipo de sesión en la tarjeta de previsualización** («pedir
  completa / reducida / recuperación»). Es lo que haría alcanzable
  `override_session_type` desde el móvil, y por tanto lo que permitiría que la
  medida (b) de calibración acumulase casos juzgables. Sin esto, la cita de
  calibración puede no llegar nunca: se puede marcar el desacuerdo, pero no
  pedir otra sesión.

  **QUEDA FUERA A PROPÓSITO, y el criterio es del usuario:** si durante estas
  semanas echa de menos poder pedir la sesión completa, eso demuestra que hace
  falta de verdad; si no lo echa de menos, es que sobraba. O sea que la
  ausencia de la medida (b) no es un fallo que arreglar en la próxima cita: es
  el experimento. Lo que hay que traer a la cita no es el control construido,
  sino la respuesta a «¿lo he echado de menos?».
- **Por qué se cortó a medias la migración de `notifications`.** El 18 de
  septiembre la tabla apareció vacía con las 5 filas reales en una
  `_vieja_notifications` que debería haberse borrado. Se copiaron y se limpió
  la tabla sobrante ese mismo día (copia previa de la base en
  `/app/data/app.db.antes-de-notifications-20260918191949`), así que el
  síntoma está resuelto. **Lo que no se ha explicado es la causa**: esa
  migración corre entera dentro de un `with eng.begin()` en
  `app/db.py:377`, o sea en una sola transacción, y SQLite hace las DDL
  transaccionales. Un corte a mitad debería haber deshecho también el
  renombrado, y no lo hizo. Hipótesis sin comprobar: el renombrado se
  confirmó en un arranque anterior y el `create_all()` de `init_db()`
  —que corre ANTES de `ensure_schema`— recreó la tabla vacía en el siguiente,
  con lo que la migración ya se vio innecesaria y no reintentó. Si es eso,
  puede repetirse con cualquier tabla la próxima vez que se toque el esquema,
  y esta vez sí podría tocarle a una que se lea. **Mirarlo en la cita del 28.**
- **Los tres READMEs y `docs/primer-dia.md`** no los ata ningún test, a
  diferencia de `docs/analisis.md`. Decisión consciente: son documentación que
  se lee a mano.
