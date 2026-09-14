/*
 * Ejecuta `comprobarSalud()` de la PWA contra un `/api/health` de mentira y
 * escupe el HTML que acaba en la caja de avisos.
 *
 * Lo arranca `tests/test_pwa.py::test_la_pantalla_pinta_de_verdad_cada_aviso_
 * que_el_servidor_sabe_marcar`, que le pasa el payload por argumento.
 *
 * POR QUÉ HACE FALTA, TENIENDO YA EL CRUCE POR TEXTO
 * --------------------------------------------------
 * El test hermano comprueba que la expresión -`stale_write`, `pending_error`-
 * APAREZCA en `app.js`. Eso pilla el olvido entero, que es el caso que se dio de
 * verdad, y no pilla el de al lado: una mutación que dejaba el bloque del aviso
 * escrito y le mataba la condición -`if (false)`- sobrevivió a la batería
 * entera. La expresión seguía estando en el archivo, dentro del cuerpo del
 * bloque, así que el `in codigo` daba verdadero y el aviso no se pintaba nunca.
 *
 * Buscar la expresión "dentro de un `if`" con una expresión regular no vale:
 * media pantalla lee sus datos en un `const plan = s.scheduler || {}` y decide
 * con la variable, no con el campo. La única forma de saber si un aviso se
 * pinta es pintarlo.
 *
 * Es el mismo argumento que `render_pwa.mjs` hace para las cinco vistas de
 * métricas, aplicado a la pantalla que avisa de las averías -que es la que
 * menos se puede permitir un aviso que no sale-.
 */

import fs from "node:fs";
import vm from "node:vm";

const RUTA = process.argv[2];
if (!RUTA) {
  console.error("uso: node tests/salud_pwa.mjs <health.json>");
  process.exit(2);
}
const salud = JSON.parse(fs.readFileSync(RUTA, "utf8"));

/* El DOM mínimo. Igual de tonto que el de `render_pwa.mjs`: recoge y no simula.
 * Lo único que interesa es el `innerHTML` de `#salud`. */
const elementos = {};
function elemento(id) {
  if (!elementos[id]) {
    elementos[id] = {
      id,
      _html: "",
      textContent: "",
      className: "",
      hidden: true,
      value: "",
      dataset: {},
      classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = v; },
      addEventListener() {},
      appendChild() {},
      prepend() {},
      querySelectorAll: () => [],
    };
  }
  return elementos[id];
}

const contexto = {
  console,
  document: {
    getElementById: elemento,
    createElement: () => ({ className: "", textContent: "" }),
    querySelectorAll: () => [],
  },
  location: { origin: "http://x", hash: "", pathname: "/" },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  addEventListener: () => {},
  setTimeout, clearTimeout,
  window: { addEventListener: () => {}, location: { pathname: "/" } },
  // Sin `serviceWorker`: el registro del final de `app.js` se salta solo, y no
  // hay nada que simular de él.
  navigator: {},
  Math, Number, String, Object, Array, JSON, Set, Map, Date, URL, Error,
  isNaN, parseInt, parseFloat, encodeURIComponent, Promise,
  async fetch(url) {
    const u = String(url);
    if (u.includes("/api/health")) {
      return { ok: true, status: 200, json: async () => salud };
    }
    // Todo lo demás -el check-in de hoy, la decisión- se corta a propósito.
    // `arrancar()` lo pinta como "no hay servidor" en SU caja, que no es ésta,
    // y así se comprueba de paso que los dos avisos no se pisan.
    throw new Error(`fuera de alcance en este arnés: ${u}`);
  },
};
contexto.globalThis = contexto;

vm.createContext(contexto);
for (const f of ["static/comun.js", "static/app.js"]) {
  vm.runInContext(fs.readFileSync(f, "utf8"), contexto, { filename: f });
}

// El fichero ya la llama al cargarse, pero sin esperarla. Se vuelve a llamar y
// se espera: el resultado es el mismo -no guarda estado- y aquí sí se sabe
// cuándo ha terminado.
await vm.runInContext("comprobarSalud()", contexto);

const caja = elementos["salud"];
console.log(JSON.stringify({
  html: caja ? caja._html : "",
  visible: caja ? caja.hidden === false : false,
}));
