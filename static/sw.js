/*
 * Service worker.
 *
 * LO QUE NO SE CACHEA, Y POR QUÉ ES LO IMPORTANTE
 * -----------------------------------------------
 * Nada que empiece por `/api/`. Ni una respuesta, ni con "network first", ni
 * como último recurso cuando no hay red.
 *
 * La tentación es evidente: guardar la última respuesta de `/api/checkin/today`
 * para que el formulario abra sin conexión. Pero esa respuesta dice si YA has
 * hecho el check-in de hoy, y servir la de ayer significa abrir el formulario
 * diciendo "ya está hecho" un día en que no lo está. El sistema se quedaría
 * esperando un envío que la pantalla da por hecho, tiraría del trabajo de
 * respaldo de las 09:00 y decidiría sin check-in. Nadie miraría por qué.
 *
 * Lo mismo con `/api/decision`: un semáforo de ayer pintado como el de hoy es
 * una mentira que no se distingue de la verdad.
 *
 * Así que se cachea SOLO el armazón -HTML, CSS, JS, iconos-, que no caduca y no
 * afirma nada. Sin conexión se abre la aplicación y se dice que no hay
 * conexión, que es lo que pasa.
 */

/* La versión sube cada vez que cambia el armazón. Si no subiera, `activate` no
 * borraría nada -el nombre del caché sería el mismo- y un móvil con la versión
 * vieja abierta se quedaría con el `metricas.js` de antes contra una API nueva.
 *
 * Y ESA FRASE DE AHÍ ARRIBA ESTUVO SIN CUMPLIRSE. `metricas.js` cambió dos
 * veces -la portada entera y los nombres de los colores- con la versión
 * clavada en "v5". No se notó, y no se notó por un motivo que conviene
 * entender: el `fetch` de abajo va A LA RED PRIMERO, así que cualquier móvil
 * con cobertura se trae el archivo nuevo igual. Lo que la versión protege es
 * el caso contrario -el móvil que estuvo sin red-, y ese no se prueba nunca
 * mirando la pantalla.
 *
 * Una regla que solo vive en un comentario es una regla que se incumple sin
 * que salte nada. Ahora la ata `tests/test_pwa.py`, que guarda la huella del
 * armazón al lado de la versión y se pone rojo si el contenido se mueve y el
 * número no. */
const VERSION = "v17";
const CACHE = `armazon-${VERSION}`;

/* TODO el armazón, no "lo principal".
 *
 * Esta lista se olvidó de crecer cuando se añadieron las métricas, y ese olvido
 * no da ningún error: online todo funciona porque el `fetch` de abajo cachea al
 * vuelo lo que se va pidiendo. Lo que se rompe es el primer arranque sin
 * cobertura de una pantalla que nunca se visitó con red: la métrica sale en
 * blanco y parece que no hay datos.
 *
 * `tests/test_pwa.py` compara esta lista contra el contenido real de `static/`
 * y falla si aparece un archivo que no está nombrado aquí. Es la única forma de
 * que el siguiente que se añada no repita exactamente esto.
 */
const ARMAZON = [
  "/",
  "/index.html",
  "/metricas.html",
  "/styles.css",
  "/app.js",
  "/comun.js",
  "/graficos.js",
  "/metricas.js",
  "/manifest.webmanifest",
  "/icons/icon.svg",
  // El maskable va también, aunque no salga en ningún `<img>` de ningún HTML:
  // lo pide el manifest, y es el que coge Android para el icono de la pantalla
  // de inicio. Sin él en el caché, instalar la aplicación sin cobertura deja el
  // icono recortado dentro de un cuadrado blanco. Faltaba, y lo encontró el
  // test: es exactamente el olvido que ese test existe para no repetir.
  "/icons/icon-maskable.svg",
  // Y los PNG, que son los que de verdad se instalan en casi todas partes: el
  // SVG en el manifest solo lo entiende un Chrome moderno, y Safari no mira el
  // manifest para esto. Los dibuja `scripts/generar_iconos.py` desde esos dos
  // SVG de aquí arriba; no se editan a mano.
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-192.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (ev) => {
  ev.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(ARMAZON))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (ev) => {
  // Las versiones viejas se van. Un armazón antiguo hablando con una API nueva
  // es un fallo raro y difícil de ver desde el móvil.
  ev.waitUntil(
    caches.keys()
      .then((claves) => Promise.all(
        claves.filter((k) => k !== CACHE).map((k) => caches.delete(k)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);

  // Ni se toca: que lo resuelva la red o que falle. Si falla, `app.js` lo dice.
  if (url.pathname.startsWith("/api/")) return;
  if (ev.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;

  // El armazón: primero la red -para que una versión nueva del contenedor se
  // note al primer arranque con cobertura- y la copia solo si no hay red.
  ev.respondWith(
    fetch(ev.request)
      .then((resp) => {
        if (resp && resp.ok) {
          const copia = resp.clone();
          caches.open(CACHE).then((c) => c.put(ev.request, copia));
        }
        return resp;
      })
      .catch(() => caches.match(ev.request).then((hit) => {
        if (hit) return hit;
        // El último recurso es una PÁGINA, así que solo vale para quien pedía
        // una página. Dárselo a un `<script src>` que no estaba en el caché
        // devolvería el HTML del check-in con `Content-Type: text/html`, el
        // navegador se negaría a ejecutarlo y la pantalla saldría en blanco sin
        // un solo error legible desde el móvil. Mejor que el `fetch` falle.
        if (ev.request.mode !== "navigate") return Response.error();
        return caches.match("/index.html");
      })),
  );
});
