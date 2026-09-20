# Instrucciones para Claude en este repositorio

Lee esto entero antes de tocar nada. No es un estilo: son las reglas con las
que está escrito todo lo que hay, y saltárselas produce cambios que pasan los
tests y rompen el sistema en producción.

## Qué es esto

Sistema de entrenamiento de fuerza adaptativo, de un solo usuario y
autoalojado. Lee Garmin (sueño, HRV, FC en reposo, carga) y Hevy (lo entrenado
de verdad), pregunta un check-in de siete deslizadores por la mañana, y decide
el color del día (verde / ámbar / rojo), la rutina que toca y las cargas. Lo
escribe en Hevy y lo cuenta por Telegram.

**El motor de decisión son reglas explícitas en `config.yaml`. Nunca un LLM, ni
un modelo, ni una heurística escondida en el código.** Si una decisión no se
puede explicar señalando una regla del YAML, está mal hecha.

El usuario tiene una **hernia discal L4-L5**. No es un detalle de contexto: es
la razón de la mitad de las decisiones de diseño, y la dirección en la que
tiene que equivocarse el sistema cuando duda.

## Idioma

**Todo en español**: comentarios, docstrings, nombres de tests, mensajes de
error, mensajes de commit. El código (nombres de funciones y variables) sigue
lo que ya haya en cada fichero, que es mezcla.

**Los mensajes de commit van SIN TILDES.** El resto lleva tildes normales.

## Cómo se escribe aquí

Este repositorio persigue un defecto concreto: **la pieza que tenía que avisar
del hueco es la que dice que no lo hay.** Un validador que certifica una
sección muerta. Una fila que dice "Sí" cuando no se escribe nada. Un documento
que dice "no implementada" con cinco vistas sirviendo. Un botón que está en el
HTML, tiene sus tests en verde, y mide 92 × 20 píxeles en gris sobre un fondo
oscuro.

De ahí salen tres reglas que no son negociables:

1. **Un test nuevo no cuenta hasta que se le ha visto rojo.** Rompe a propósito
   lo que el test vigila, comprueba que falla y con qué mensaje, y restaura.
   Para una guarda con varias ramas, un banco de mutaciones. Si una mutación no
   la caza nadie, el test no sirve todavía.
2. **Ningún valor decorativo.** Si se declara una clave, una excepción, un
   `salvo`, un umbral o una lista, hay que comprobar *de oficio* que alguien lo
   lee de verdad. Una excepción que ya no excusa nada es peor que ninguna:
   sigue firmando un permiso. Esto aprieta más en las reglas de seguridad.
3. **Los comentarios explican POR QUÉ, y envejecen.** Cuando un cambio deja
   falso un comentario que está en otro sitio, se corrige en el mismo commit.
   Un comentario que contradice al código es el defecto de arriba otra vez.

Los comentarios de este repositorio son largos a propósito y cuentan el fallo
concreto que motivó cada pieza, con fecha. Sigue ese registro: no escribas
"validamos la entrada", escribe qué pasó el día que no se validaba.

## Trampas del entorno

- **Windows.** El intérprete es `.venv\Scripts\python.exe`, nunca `python`.
- **Nunca escribas ficheros con `Set-Content -Encoding UTF8`** en PowerShell
  5.1: corrompe las tildes. Usa las herramientas de edición del editor, o
  Python con `encoding="utf-8"` explícito.
- La salida de consola es cp1252: para ejecutar scripts que impriman tildes o
  símbolos, `PYTHONIOENCODING=utf-8`.
- Tests: `.venv\Scripts\python.exe -m pytest -q -p no:randomly`. Tardan unos
  cuatro minutos. Son ~2670 y tienen que quedar todos en verde.

## Despliegue: la divergencia que hay que tener siempre en la cabeza

- **`config.yaml` va bind-mounted**: un cambio en el fichero afecta al sistema
  en marcha en cuanto se recrea el contenedor, sin reconstruir la imagen.
- **El código va horneado en la imagen**: no cambia hasta reconstruir.
- **Reconstruir es decisión del usuario, no tuya.** Propónselo diciendo qué
  entra además de lo de hoy.
- **Añadir una clave nueva a `config.yaml` rompe el contenedor que está
  corriendo**, porque su validador no la conoce y la rechaza al recargar.
  Primero el código, luego la reconstrucción, y solo después la clave.
- La base de datos del contenedor **no es `data/` del repositorio**: es un
  volumen Docker con nombre. Para contar filas de verdad, `docker exec`.

## Las guardas que se disparan solas, y qué pedirán de ti

- **Huella del armazón.** Cualquier cambio en `static/` obliga a subir
  `VERSION` en `static/sw.js` y a añadir una entrada a `HUELLAS_DEL_ARMAZON` en
  `tests/test_pwa.py` con un comentario que clasifique cómo falla un móvil que
  se quede con la versión vieja (LEVE / GRAVE / callada). El test te dará la
  huella exacta en el mensaje de fallo.
- **Estilos.** `tests/test_estilos.py` exige que toda clase que la pantalla
  escriba esté nombrada en algún selector de `styles.css`, y que todo elemento
  cuyo único nombre sea su `id` tenga regla. Existe porque un botón entero
  nació sin una sola línea de CSS y nadie se enteró en dos semanas.
- **Dobles de test.** Toda clase definida en `tests/` lleva `@doble_de(...)` o
  `@no_es_doble("motivo")`. Un `salvo=` que ya no excusa nada se rechaza.
- **Documentación.** `docs/analisis.md` está atado a las rutas que sirve
  FastAPI: nombrar un endpoint que no existe, u omitir uno que sí, pone rojo
  `tests/test_docs.py`.
- **Despliegue.** `tests/test_despliegue.py` compara `umbrel/docker-compose.yml`
  y `.dockerignore` contra lo que el sistema necesita de verdad (el puerto sale
  del `CMD`, el UID del `useradd`, los montajes de las rutas de `Settings`).

## Añadir una vista a la PWA: los siete sitios

Se registra en siete puntos y olvidar uno no da error, da una pantalla a la que
no se llega o un hueco en blanco:

`RUTAS` y `VISTAS` en `static/metricas.js` · `PANTALLAS` en `static/comun.js` ·
`RUTAS`/`VISTAS` en `tests/render_pwa.mjs` · `rutas` en `_payloads` de
`tests/test_pwa.py` · `POR_VISTA` en `app/analysis/encabezados.py` ·
`docs/analisis.md`.

## Arneses de JavaScript

No hay `jsdom` ni `package.json`, y no los va a haber: la batería tiene que
correr con `node` a secas. Los arneses (`tests/*.mjs`) leen un guion JSON por
argumento, montan su propio DOM mínimo, ejecutan el fichero real de `static/`
con `vm.runInContext` e imprimen una línea JSON. Sigue ese patrón.

## Seguridad

`safety.forbidden_in_hiit` en `config.yaml` bloquea por `template_id` y por
patrón de nombre los ejercicios que no pueden entrar en un HIIT con una hernia
L4-L5. **No lo ensanches para arreglar un falso positivo**: se añade el
ejercicio concreto a `allow_exceptions` con el motivo escrito, que es donde
queda auditable.

## Dónde mirar antes de preguntar

- `docs/analisis.md` — las vistas de métricas y sus endpoints.
- `docs/cita-de-recalibracion.md` — en qué estado está el sistema, qué está
  esperando datos, qué es normal aunque parezca roto.
- `umbrel/README.md` — publicar la imagen e instalar como aplicación de Umbrel.
- `docs/primer-dia.md` — la puesta en marcha.
