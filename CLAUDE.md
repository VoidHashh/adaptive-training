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

## Despliegue: donde vive el sistema, y como llega hasta alli un cambio

**El sistema en marcha es el Umbrel** (`192.168.8.188`), aplicación
`planb-adaptive-training`, desde el 25/09/2026. El contenedor del PC quedó
**parado a propósito, no borrado**: su volumen `hevy2garmin-test_datos-pruebas`
es la vuelta atrás. No lo borres, y no lo levantes sin apagar el Umbrel antes
—dos planificadores sobre la misma cuenta son dos decisiones pisándose el mismo
día y dos escrituras de la misma rutina en Hevy—.

**UN CAMBIO EN EL CÓDIGO NO LLEGA SOLO.** Son cinco pasos y ninguno es
opcional; saltarse uno no da error, da un sistema que sigue corriendo lo de
antes mientras el repositorio dice otra cosa:

1. **Commit y `git push`.** La rama local es `master` y la remota `main`; está
   puesto `push.default = upstream`, así que `git push` a secas vale.
2. **El taller publica la imagen.** `.github/workflows/docker.yml` construye y
   sube a `ghcr.io/voidhashh/adaptive-training` en cada empujón. Esto es lo
   único automático de la lista.
3. **Subir la versión, en los cuatro sitios a la vez**: `pyproject.toml`,
   `image:` de `umbrel/docker-compose.yml`, `version:` de
   `umbrel/umbrel-app.yml` y `image:` del compose de la raíz.
   `tests/test_despliegue.py` se pone rojo si falta uno. **Sin subirla, el
   Umbrel NO vuelve a descargar nada**: la etiqueta ya la tiene, sigue
   corriendo lo instalado, y desde el móvil eso se parece exactamente a que el
   arreglo está puesto.
4. **Copiar `umbrel/umbrel-app.yml` y `umbrel/docker-compose.yml` al
   repositorio de la tienda**, `VoidHashh/PlanB`, carpeta
   `planb-adaptive-training/`. Umbrel lee de ahí, no de aquí. **Ningún test
   avisa si no se hace**, porque el test no ve el otro repositorio.
5. **Actualizar desde la interfaz de Umbrel.** Eso lo hace el usuario: no hay
   acceso a Docker en esa máquina (el usuario `umbrel` no está en el grupo
   `docker` y `sudo` pide contraseña).

**`config.yaml` es la excepción y sigue siéndolo.** Va bind-mounted en
`${APP_DATA_DIR}/config.yaml` y se aplica al recrear el contenedor, sin imagen
nueva. Pero **añadir una clave nueva rompe el contenedor que está corriendo**,
porque su validador no la conoce y la rechaza al recargar: primero el código,
luego la actualización, y solo después la clave.

**Para saber qué código corre, `/api/health` devuelve `build.sha`.** La
etiqueta no sirve para eso: lleva congelada en `0.1.0` desde el principio, y de
ahí que el taller pase `BUILD_SHA` como `--build-arg`.

Los datos del Umbrel están en `~/umbrel/app-data/planb-adaptive-training/data/`
y se leen por SSH (`ssh umbrel`), no por `docker exec`. Ahí dentro está
`hevy_backups/`, que es lo único que permite deshacer una escritura en Hevy.

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
