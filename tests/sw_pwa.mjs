/*
 * Ejecuta `static/sw.js` DE VERDAD y enseña qué se escribió en el caché.
 *
 * Lo arranca `tests/test_pwa.py`, que le pasa por argumento un fichero con lo
 * que hay precargado en cada caché, qué contesta la red a cada ruta y la lista
 * de peticiones que hay que despachar. Devuelve por stdout, en la última línea,
 * qué salió de cada petición y -lo único que de verdad importa- TODAS las
 * operaciones de escritura sobre `CacheStorage`, con la fase en la que
 * ocurrieron.
 *
 * POR QUÉ ESTE ARNÉS Y NO UNA BÚSQUEDA DE TEXTO
 * ---------------------------------------------
 * Lo que hay que demostrar es que una petición del armazón no escribe en el
 * caché. Un `assert "caches.put" not in SW` no vale: el `put` sigue estando en
 * el archivo, y tiene que seguir estando, porque el camino de lo que NO es
 * armazón lo conserva a propósito. Lo que cambió no es si la llamada existe,
 * es POR QUÉ RAMA se pasa, y una rama no se lee con una expresión regular.
 *
 * Y hay un fallo peor que una regla incumplida: una regla que se cumple hoy y
 * se descumple mañana sin mover la línea vigilada. Bastaría con que alguien
 * sacara `esArmazon` del `if` o le cambiara el orden a los cortes para que el
 * armazón volviera a caer en el camino de abajo, con el `put` intacto en su
 * sitio y la búsqueda de texto igual de verde. Así que se despacha el evento y
 * se mira quién tocó el caché, que es el acto y no el síntoma.
 *
 * LO QUE ESTE ARNÉS PUEDE DEMOSTRAR Y UNA LECTURA NO
 * ---------------------------------------------------
 * La mezcla de generaciones, que es el fallo entero. Se precarga el caché con
 * la generación VIEJA, se pone la red a servir la NUEVA, y se piden dos
 * archivos del armazón con la red fallando en medio. Si el `put` volviera, el
 * primero devolvería nuevo, se guardaría, y el segundo -sin red- saldría viejo
 * del mismo caché: dos generaciones en la misma pantalla. Eso no se ve leyendo
 * el archivo por mucho que se lea.
 *
 * LOS DOBLES DE AQUÍ ABAJO
 * ------------------------
 * No hay `jsdom` ni `package.json` en este repositorio a propósito -la batería
 * tiene que correr con `node` a secas-, así que van escritos aquí. Son tontos
 * pero honrados en lo que importa: `addAll` hace `fetch` de verdad de cada ruta
 * y es ATÓMICO -si una falla no entra ninguna-, que es la propiedad sobre la
 * que se apoya todo el diseño nuevo; `caches.match` busca en TODOS los cachés,
 * como el de verdad, que es lo que hace que borrar el viejo en `activate`
 * signifique algo.
 */

import fs from "node:fs";
import vm from "node:vm";

const RUTA = process.argv[2];
if (!RUTA) {
  console.error("uso: node tests/sw_pwa.mjs <guion.json>");
  process.exit(2);
}
const guion = JSON.parse(fs.readFileSync(RUTA, "utf8"));
const ORIGEN = guion.origen || "http://localhost:8000";

// ---------------------------------------------------------------------------
// El registro: quién tocó el caché, y en qué fase
// ---------------------------------------------------------------------------
//
// La FASE es la mitad del dato. Un `addAll` durante `install` es el diseño; un
// `put` durante una petición es el fallo. Sin la fase, las dos cosas son "una
// escritura en el caché" y el test tendría que contarlas, que es como se
// escriben los tests que se rompen al añadir un icono.

let fase = "arranque";
const escrituras = [];
const anotar = (tipo, cache, que) => escrituras.push({ fase, tipo, cache, que });

// ---------------------------------------------------------------------------
// Peticiones y respuestas
// ---------------------------------------------------------------------------

class PeticionDoble {
  constructor({ url, method = "GET", mode = "no-cors" }) {
    this.url = url;
    this.method = method;
    this.mode = mode;
  }
}

/* La `marca` es la generación, y es lo que convierte este arnés en una prueba.
 *
 * Un cuerpo cualquiera diría si la respuesta llegó; la marca dice DE CUÁNDO es.
 * Sin ella, "sirvió algo" y "sirvió lo coherente" son el mismo verde. */
class RespuestaDoble {
  constructor({ ok = true, status = 200, marca = null, url = null }) {
    this.ok = ok;
    this.status = status;
    this.marca = marca;
    this.url = url;
  }
  clone() {
    return new RespuestaDoble(this);
  }
  static error() {
    return new RespuestaDoble({ ok: false, status: 0, marca: "error-de-red" });
  }
}

// Qué contesta la red a cada ruta, y qué generación sirve.
//
// `guion.red` es un objeto {ruta: "ok"|"fallo"}. Lo que no esté nombrado cae en
// `guion.red_por_defecto`, para no tener que escribir las quince del armazón en
// cada caso.
const fetches = [];
function fetchDoble(peticion) {
  const p = peticion instanceof PeticionDoble
    ? peticion
    : new PeticionDoble({ url: String(peticion) });
  const ruta = new URL(p.url, ORIGEN).pathname;
  fetches.push({ fase, ruta });
  const estado = guion.red && ruta in guion.red
    ? guion.red[ruta]
    : (guion.red_por_defecto || "ok");
  if (estado === "fallo") {
    return Promise.reject(new TypeError(`sin red para ${ruta}`));
  }
  if (estado === "404") {
    return Promise.resolve(
      new RespuestaDoble({ ok: false, status: 404, marca: guion.marca_red, url: ruta }),
    );
  }
  return Promise.resolve(
    new RespuestaDoble({ ok: true, status: 200, marca: guion.marca_red, url: ruta }),
  );
}

// ---------------------------------------------------------------------------
// CacheStorage
// ---------------------------------------------------------------------------

const clave = (req) =>
  new URL(req instanceof PeticionDoble ? req.url : String(req), ORIGEN).pathname;

class CacheDoble {
  constructor(nombre) {
    this.nombre = nombre;
    this.mapa = new Map();
  }

  async put(req, resp) {
    anotar("put", this.nombre, clave(req));
    this.mapa.set(clave(req), resp);
  }

  /* Atómico, como el de verdad. Es la propiedad sobre la que se apoya el diseño
   * entero -"el caché se escribe de una vez o no se escribe"-, así que un doble
   * que guardara las que fueran saliendo dejaría sin comprobar justo lo que se
   * quiere afirmar: que no puede quedar medio caché de una versión. */
  async addAll(rutas) {
    anotar("addAll", this.nombre, [...rutas]);
    const respuestas = await Promise.all(rutas.map((r) => fetchDoble(r)));
    if (respuestas.some((r) => !r.ok)) {
      throw new TypeError("addAll: alguna petición no salió bien");
    }
    rutas.forEach((r, i) => this.mapa.set(clave(r), respuestas[i]));
  }

  async match(req) {
    return this.mapa.get(clave(req));
  }
}

const almacenes = new Map();
const borrados = [];
const caches = {
  async open(nombre) {
    if (!almacenes.has(nombre)) almacenes.set(nombre, new CacheDoble(nombre));
    return almacenes.get(nombre);
  },
  async keys() {
    return [...almacenes.keys()];
  },
  async delete(nombre) {
    borrados.push({ fase, cache: nombre });
    return almacenes.delete(nombre);
  },
  // Busca en TODOS los cachés, como el de verdad. Si buscara solo en el de la
  // versión de ahora, borrar el viejo en `activate` no cambiaría nada y el test
  // de que se borra no probaría nada.
  async match(req) {
    for (const c of almacenes.values()) {
      const hit = await c.match(req);
      if (hit) return hit;
    }
    return undefined;
  },
};

// Lo que ya había en el caché antes de arrancar: {"armazon-v18": {"/app.js": "vieja"}}
for (const [nombre, contenido] of Object.entries(guion.precargado || {})) {
  const c = new CacheDoble(nombre);
  for (const [ruta, marca] of Object.entries(contenido)) {
    c.mapa.set(ruta, new RespuestaDoble({ marca, url: ruta }));
  }
  almacenes.set(nombre, c);
}
// Lo precargado no es una escritura del service worker: es el estado de partida.
escrituras.length = 0;

// ---------------------------------------------------------------------------
// El evento y el `self`
// ---------------------------------------------------------------------------

class Evento {
  constructor(tipo, peticion) {
    this.type = tipo;
    this.request = peticion;
    this.esperas = [];
    this.respuesta = undefined;
    this.respondida = false;
  }
  waitUntil(p) {
    this.esperas.push(p);
  }
  respondWith(p) {
    // Que se llame dos veces es un error real del navegador, y callarlo aquí
    // dejaría pasar un `respondWith` duplicado como si fuera normal.
    if (this.respondida) throw new Error("respondWith llamado dos veces");
    this.respondida = true;
    this.respuesta = p;
  }
}

const oyentes = new Map();
const hitos = { skipWaiting: false, claim: false };

const contexto = {
  console,
  URL,
  Promise,
  Map,
  Set,
  TypeError,
  Error,
  JSON,
  Object,
  Array,
  Response: RespuestaDoble,
  Request: PeticionDoble,
  caches,
  fetch: fetchDoble,
  self: {
    addEventListener(tipo, fn) {
      if (!oyentes.has(tipo)) oyentes.set(tipo, []);
      oyentes.get(tipo).push(fn);
    },
    location: { origin: ORIGEN },
    async skipWaiting() {
      hitos.skipWaiting = true;
    },
    clients: {
      async claim() {
        hitos.claim = true;
      },
    },
  },
};
contexto.globalThis = contexto;

vm.createContext(contexto);
vm.runInContext(fs.readFileSync("static/sw.js", "utf8"), contexto, {
  filename: "static/sw.js",
});

const VERSION = vm.runInContext("VERSION", contexto);
const CACHE = vm.runInContext("CACHE", contexto);
const ARMAZON = vm.runInContext("JSON.stringify(ARMAZON)", contexto);

// Deja correr los `.then` sueltos. El `put` del camino de abajo NO va colgado de
// `respondWith` -es un `caches.open(...).then(...)` que nadie espera-, así que
// sin drenar aquí el arnés podría mirar el registro antes de que se escribiera y
// dar por bueno justo lo que busca.
const drenar = async () => {
  for (let i = 0; i < 5; i += 1) await new Promise((r) => setTimeout(r, 0));
};

async function despachar(tipo, ev) {
  for (const fn of oyentes.get(tipo) || []) fn(ev);
  await Promise.all(ev.esperas.map((p) => p.catch((e) => ({ error: String(e) }))));
  await drenar();
}

// ---------------------------------------------------------------------------
// La secuencia
// ---------------------------------------------------------------------------

const salida = { version: VERSION, cache: CACHE, armazon: JSON.parse(ARMAZON) };

if (guion.instalar !== false) {
  fase = "install";
  const ev = new Evento("install");
  await despachar("install", ev);
  salida.instalacion = {
    // Qué quedó dentro del caché de esta versión, que es la única forma de
    // distinguir "addAll se llamó" de "addAll entró".
    cacheado: [...(almacenes.get(CACHE)?.mapa.keys() || [])].sort(),
    skip_waiting: hitos.skipWaiting,
  };
}

if (guion.activar !== false) {
  fase = "activate";
  const ev = new Evento("activate");
  await despachar("activate", ev);
  salida.activacion = {
    cachés: [...almacenes.keys()].sort(),
    borrados: borrados.filter((b) => b.fase === "activate").map((b) => b.cache),
    claim: hitos.claim,
  };
}

salida.peticiones = [];
for (const p of guion.peticiones || []) {
  fase = `peticion:${p.url}`;
  const antes = fetches.length;
  const ev = new Evento(
    "fetch",
    new PeticionDoble({
      url: new URL(p.url, ORIGEN).href,
      method: p.method || "GET",
      mode: p.mode || "no-cors",
    }),
  );
  for (const fn of oyentes.get("fetch") || []) fn(ev);

  let resultado = null;
  let fallo = null;
  if (ev.respondida) {
    try {
      const resp = await ev.respuesta;
      resultado = resp === undefined
        ? { vacia: true }
        : { ok: resp.ok, status: resp.status, marca: resp.marca ?? null };
    } catch (e) {
      fallo = String(e);
    }
  }
  await drenar();

  salida.peticiones.push({
    url: p.url,
    // `false` quiere decir que el service worker NO se metió: el navegador va a
    // la red por su cuenta. Es lo que tiene que pasar con `/api/`.
    interceptada: ev.respondida,
    resultado,
    fallo,
    // Las escrituras de ESTA petición, que es la pregunta del test.
    escrituras: escrituras.filter((e) => e.fase === fase),
    fue_a_la_red: fetches.slice(antes).map((f) => f.ruta),
  });
}

salida.escrituras = escrituras;
// Todas las escrituras que NO son la instalación. Es el número que el test mira
// de un vistazo: el caché se llena una vez y nunca más.
salida.escrituras_fuera_de_install = escrituras.filter((e) => e.fase !== "install");

console.log(JSON.stringify(salida));
