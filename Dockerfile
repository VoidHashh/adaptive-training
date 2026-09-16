# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Construcción
# ---------------------------------------------------------------------------
# Dos etapas para que el compilador no viaje en la imagen final. `uvloop` y
# `httptools` (los trae `uvicorn[standard]`) no siempre tienen rueda compilada
# para ARM64, que es lo que hay debajo de la mayoría de los Umbrel, y sin
# `build-essential` el build falla ahí y solo ahí.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# El entorno virtual se copia entero a la etapa final. Es la forma más simple de
# quedarse con las dependencias y no con lo que hizo falta para instalarlas.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Solo el `pyproject.toml`: mientras no cambien las dependencias, este paso sale
# de la caché aunque se toque el código. En un Raspberry Pi eso son minutos.
#
# Se instala el proyecto (para que pip resuelva las dependencias desde el
# `pyproject.toml`, que es el único sitio donde están escritas) y acto seguido se
# DESINSTALA el paquete, dejando solo las dependencias. Sin ese `pip uninstall`
# quedaría en `site-packages` un paquete `app` vacío -aquí solo existe el
# `__init__.py` de mentira- que taparía al de verdad en `/app`, y el contenedor
# moriría al arrancar con un `ModuleNotFoundError: app.api` imposible de
# entender mirando el código.
WORKDIR /src
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install . \
    && pip uninstall -y adaptive-training

# ---------------------------------------------------------------------------
# Imagen final
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# `PYTHONUNBUFFERED` no es cosmético aquí: sin él los logs se quedan en el búfer
# y `docker logs` no enseña nada hasta que el proceso muere. En un sistema que
# solo se mira cuando algo ha ido mal, eso es no tener logs.
# `PYTHONPATH=/app` explícito. `uvicorn` mete el directorio actual en `sys.path`
# por su cuenta, así que sin esto también funcionaría, pero funcionaría por un
# detalle de uvicorn en vez de porque alguien lo haya decidido.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

# `REPO_ROOT` es el padre del paquete `app`, o sea `/app`. De ahí cuelgan por
# defecto la base de datos, los tokens de Garmin, la caché de actividades y las
# copias de las rutinas de Hevy: TODO en `/app/data`, que es el único volumen.
# Un solo punto de montaje a propósito. La ruta de la caché de actividades está
# escrita en el código, así que repartir estas cosas entre varios volúmenes
# acabaría con la base de datos persistida y la caché dentro de la imagen,
# perdiéndose en cada actualización y forzando un login a Garmin -y sus 429- en
# cada arranque.
WORKDIR /app
COPY app/ ./app/
COPY static/ ./static/
COPY config.yaml ./config.yaml

# LA MARCA DE QUÉ CÓDIGO ES ESTE. Va DESPUÉS de copiar el código a propósito:
# cambia en cada build y aquí no invalida ninguna capa cara.
#
# Sin esto, «¿el arreglo de ayer ya está corriendo?» no se puede contestar
# mirando el sistema, y esa pregunta se hace después de CADA arreglo. La
# etiqueta de la imagen no vale: lleva cuarenta commits en `0.1.0` y no se mueve.
#
# El defecto vacío no es descuido: `/api/health` distingue «no se sabe» de un
# valor, y prefiere decirlo a inventarse uno. Construir sin pasar `GIT_SHA`
# sigue funcionando y sigue siendo honesto.
#
# El argumento se llama igual que la variable que acaba leyendo `Settings`. Dos
# nombres para el mismo valor es exactamente como se desincroniza una pareja.
#
#   docker build --build-arg BUILD_SHA="$(git rev-parse --short HEAD)" \
#                --build-arg BUILD_DATE="$(date -Iseconds)" ...
ARG BUILD_SHA=""
ARG BUILD_DATE=""
ENV BUILD_SHA=$BUILD_SHA \
    BUILD_DATE=$BUILD_DATE

# Sin privilegios. El UID 1000 no es capricho: es el que Umbrel le pone a la
# carpeta de datos de la aplicación, y con otro el contenedor arranca y luego no
# puede escribir su propia base de datos.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

VOLUME ["/app/data"]
EXPOSE 8000

# El healthcheck mira que conteste, no que esté encendido. `curl` no viene en
# `slim` y no merece la pena instalarlo teniendo Python delante.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=8).status == 200 else 1)"]

# UN SOLO PROCESO. Sin `--workers`: el planificador vive dentro de la
# aplicación, así que dos trabajadores son dos planificadores decidiendo el
# mismo día sobre la misma base y pisándose. Para lo que hace esto -un
# formulario al día desde un móvil- uno sobra.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
