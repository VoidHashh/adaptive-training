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
 * entender: el `fetch` de abajo iba A LA RED PRIMERO también para el armazón,
 * así que cualquier móvil con cobertura se traía el archivo nuevo igual. Lo que
 * la versión protegía era el caso contrario -el móvil que estuvo sin red-, y
 * ese no se prueba nunca mirando la pantalla.
 *
 * DESDE LA v19 ESO YA NO ES ASÍ, y conviene leerlo aquí antes que abajo: el
 * armazón se sirve del caché de su versión y no se reescribe al vuelo, o sea
 * que la versión ya no protege un caso raro, es EL ÚNICO camino por el que
 * entra un archivo nuevo. Dejarla clavada ahora no deja al móvil sin red con lo
 * viejo: deja a todos los móviles con lo viejo, con cobertura o sin ella, hasta
 * que alguien la suba. Es más frágil a propósito, porque el fallo pasa de
 * callado a total.
 *
 * Una regla que solo vive en un comentario es una regla que se incumple sin
 * que salte nada. Ahora la ata `tests/test_pwa.py`, que guarda la huella del
 * armazón al lado de la versión y se pone rojo si el contenido se mueve y el
 * número no. */
const VERSION = "v28";
const CACHE = `armazon-${VERSION}`;

/* TODO el armazón, no "lo principal".
 *
 * Esta lista se olvidó de crecer cuando se añadieron las métricas, y ese olvido
 * no da ningún error: online todo funciona porque lo que NO está aquí se va por
 * el camino de abajo, que sigue cacheando al vuelo lo que se va pidiendo. Lo que
 * se rompe es el primer arranque sin cobertura de una pantalla que nunca se
 * visitó con red: la métrica sale en blanco y parece que no hay datos.
 *
 * Desde la v19 el olvido cuesta una segunda cosa, y es la que no se ve: un
 * archivo que falte de aquí no solo se queda fuera de `addAll`, sino que cae en
 * el único camino que todavía escribe pieza a pieza, o sea el que puede acabar
 * con una copia de otra generación al lado de las de `install`. Estar en esta
 * lista no es "además se precarga": es estar dentro de lo que se versiona.
 *
 * `tests/test_pwa.py` compara esta lista contra el contenido real de `static/`
 * y falla si aparece un archivo que no está nombrado aquí. Es la única forma de
 * que el siguiente que se añada no repita exactamente esto.
 */
const ARMAZON = [
  "/",
  "/index.html",
  "/metricas.html",
  "/despues.html",
  "/avanzado.html",
  "/styles.css",
  "/app.js",
  "/comun.js",
  "/graficos.js",
  "/metricas.js",
  "/despues.js",
  "/avanzado.js",
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

/* EL ARMAZÓN SE SIRVE DEL CACHÉ VERSIONADO Y NO SE ESCRIBE AL VUELO.
 *
 * Aquí ponía «primero la red», y con la copia guardada al vuelo: cada respuesta
 * buena se metía en `CACHE` con un `put` suelto. Eso mezclaba generaciones, y la
 * mezcla es peor que cualquiera de las dos versiones por separado.
 *
 * `CACHE` es el del service worker ACTIVO, que durante una actualización es
 * todavía el viejo -el nuevo está instalado y esperando-. Un móvil con el SW
 * anterior activo que se conecta contra el contenedor nuevo se trae por red el
 * JavaScript nuevo y lo escribe dentro de `armazon-v17`. Si en esa misma sesión
 * otra petición del armazón falla por cobertura intermitente, el `catch` sacaba
 * del mismo caché la copia VIEJA de ese otro archivo. Resultado: `metricas.js`
 * de la v18 y `comun.js` de la v17 conviviendo en un caché que se llama v17, y
 * una pantalla pintada a medias sin un solo error en la consola.
 *
 * No es hipotético: es exactamente el modo de fallo que la v18 tuvo que apuntar
 * en `HUELLAS_DEL_ARMAZON` -`metricas.js` nuevo contra `comun.js` viejo imprime
 * la frase de cobertura de percepción en la pantalla de calibración-. Lo que
 * aquella nota describía como «el móvil a medias» lo FABRICABA este `put`.
 *
 * Así que el caché se escribe de una sola vez y nunca pieza a pieza: lo llena
 * `install` con `addAll`, que es atómico -o entran los quince archivos o no
 * entra ninguno-, y `activate` cambia de caché entero. La frescura la da
 * exclusivamente la subida de `VERSION`, que es lo que `tests/test_pwa.py` ata a
 * la huella del contenido. Dos generaciones ya no caben en un caché porque un
 * caché solo se llena una vez.
 *
 * LO QUE SE PIERDE A CAMBIO, dicho y no escondido: el contenedor nuevo ya no se
 * nota en el arranque que está en curso. Antes, con cobertura, el archivo nuevo
 * llegaba por red en la misma sesión; ahora se sirve el del caché hasta que el
 * service worker nuevo termine de instalarse y `activate` cambie de caché, o
 * sea en el SIGUIENTE arranque. Se cambia inmediatez por coherencia, y es el
 * cambio bueno: una pantalla un arranque más vieja se lee entera y dice la
 * verdad de su versión; una pantalla mezclada no se distingue de una correcta.
 *
 * Y lo que NO cambia es lo de arriba del todo: bajo `/api/` no se cachea nada,
 * ni aquí ni en el camino de abajo. Un `/api/checkin/today` de ayer abre el
 * formulario diciendo "ya está hecho" un día en que no lo está. */
self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);

  // Ni se toca: que lo resuelva la red o que falle. Si falla, `app.js` lo dice.
  if (url.pathname.startsWith("/api/")) return;
  if (ev.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;

  // El armazón, del caché de su versión. Sin `put`: lo que hay es lo que metió
  // `install`, y lo que metió `install` es de una sola generación.
  const esArmazon = ARMAZON.includes(url.pathname);
  if (esArmazon) {
    ev.respondWith(caches.match(ev.request).then((hit) => hit || fetch(ev.request)));
    return;
  }

  // Lo que no está en `ARMAZON` sigue como estaba: red primero y la copia solo
  // si no hay red. Aquí el `put` no mezcla generaciones porque no hay dos
  // generaciones de nada -no es armazón, no lo llena `install` y no lo versiona
  // nadie-, y sin él un recurso que no esté en la lista no tendría copia
  // ninguna sin cobertura.
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
