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

const VERSION = "v1";
const CACHE = `armazon-${VERSION}`;

const ARMAZON = [
  "/",
  "/index.html",
  "/styles.css",
  "/app.js",
  "/manifest.webmanifest",
  "/icons/icon.svg",
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
      .catch(() => caches.match(ev.request).then(
        (hit) => hit || caches.match("/index.html"),
      )),
  );
});
