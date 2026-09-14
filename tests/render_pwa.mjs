/*
 * Pinta las seis vistas de la PWA en Node, contra payloads de verdad, y busca
 * lo que un renderizador hace cuando lee una clave que el backend no manda.
 *
 * Lo arranca `tests/test_pwa.py::test_los_renderizadores_no_leen_ni_una_clave_
 * que_el_backend_no_mande`, que antes le escribe las siete respuestas de
 * `/api/metrics/*` en el JSON que se le pasa por argumento. Aquí no se toca la
 * base ni la red: entra un JSON, salen las seis pantallas.
 *
 * POR QUÉ ESTO Y NO UN `node --check`
 * -----------------------------------
 * `node --check` dice que el archivo parsea. Lo que hace falta saber es otra
 * cosa: si `f.lectura` existía. Una clave mal adivinada no da ningún error en
 * JavaScript. Escribe la palabra "undefined" en medio de una frase -y desde el
 * móvil eso parece parte del texto- o, peor, se lee dentro de un `if` y no deja
 * ningún rastro: elige la rama contraria y la pantalla afirma, con una frase
 * perfectamente escrita, lo contrario de lo que pasa.
 */

import fs from "node:fs";
import vm from "node:vm";

const RUTA_PAYLOADS = process.argv[2];
if (!RUTA_PAYLOADS) {
  console.error("uso: node tests/render_pwa.mjs <payloads.json>");
  process.exit(2);
}
const payloads = JSON.parse(fs.readFileSync(RUTA_PAYLOADS, "utf8"));

/* Cada lectura de una clave que el payload NO trae, apuntada.
 *
 * Es la comprobación que de verdad hace falta. Buscar la palabra "undefined" en
 * el HTML solo pilla las claves inventadas que acaban impresas; la que se leyó
 * dentro de un `if` no deja rastro. Así se cazó `v.length === 2` sobre un
 * `{desde, hasta}`: no imprimía nada raro, elegía la rama contraria y escribía
 * "sin ningún dato de Garmin" encima de ciento setenta y nueve días.
 *
 * Solo se vigilan los objetos. En un array, `length`, `map` y `filter` son
 * lecturas legítimas y constantes, y apuntarlas ahogaría lo que importa.
 */
const inventadas = new Set();
const PERMITIDAS = new Set([
  "then", "constructor", "toJSON", "valueOf", "toString", "length",
  "inspect", "nodeType", "hasOwnProperty",
]);

function vigilar(v, ruta) {
  if (v === null || typeof v !== "object") return v;
  if (Array.isArray(v)) return v.map((x) => vigilar(x, `${ruta}[]`));
  const copia = {};
  for (const [k, x] of Object.entries(v)) copia[k] = vigilar(x, `${ruta}.${k}`);
  return new Proxy(copia, {
    get(t, p) {
      if (typeof p === "string" && !(p in t) && !PERMITIDAS.has(p)) {
        inventadas.add(`${ruta}.${p}`);
      }
      return t[p];
    },
  });
}

/* El DOM mínimo que los renderizadores tocan.
 *
 * A propósito tonto: no simula nada, solo recoge. Lo que interesa es el HTML
 * que se le entrega a `#vista`, y un DOM de verdad -jsdom- añadiría una
 * dependencia y una capa de comportamiento propio entre el fallo y el test.
 */
const salidas = {};
const elementos = {};
function elemento(id) {
  if (!elementos[id]) {
    elementos[id] = {
      id,
      _html: "",
      textContent: "",
      className: "",
      hidden: false,
      value: id === "dias" ? "180" : "",
      options: [{ value: "180" }],
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = v; if (id === "vista") salidas[vistaEnCurso] = v; },
      addEventListener() {},
      appendChild() {},
      prepend() {},
    };
  }
  return elementos[id];
}

let vistaEnCurso = null;

const RUTAS = {
  portada: "/api/metrics/portada",
  concordancia: "/api/metrics/concordancia",
  desfase: "/api/metrics/desfase",
  impacto: "/api/metrics/impacto",
  ranking: "/api/metrics/ranking-ejercicios",
  auditoria: "/api/metrics/auditoria",
  percepcion: "/api/metrics/percepcion",
};

const contexto = {
  console,
  document: {
    getElementById: elemento,
    createElement: () => ({ className: "", textContent: "" }),
  },
  location: { origin: "http://x", hash: "" },
  localStorage: { getItem: () => null, setItem: () => {} },
  addEventListener: () => {},
  window: { addEventListener: () => {} },
  navigator: {},
  Math, Number, String, Object, Array, JSON, Set, Map, Date, URL, Error,
  isNaN, parseInt, parseFloat,
  async fetch(url) {
    const u = String(url);
    // `ranking-ejercicios` contiene a `ranking`, así que se coge la coincidencia
    // MÁS LARGA: con la primera que valga, una ruta nueva que sea prefijo de
    // otra serviría el payload equivocado sin decir nada.
    const clave = Object.entries(RUTAS)
      .filter(([, r]) => u.includes(r))
      .sort((a, b) => b[1].length - a[1].length)[0]?.[0];
    if (!clave) throw new Error(`ruta no prevista: ${u}`);

    /* EL RANKING SE PIDE PARA UNA RESPUESTA CONCRETA, y aquí hay una sola.
     *
     * El cliente ya no la lleva clavada: la saca de `respuesta_por_defecto`,
     * que decide el servidor. Si sirviéramos este payload pase lo que pase, el
     * andamio pintaría tan contento el ranking del dolor lumbar debajo de una
     * vista de HRV y el test diría «ok». Que las dos respuestas tengan que
     * coincidir es justo lo que convierte este doble en una prueba. */
    if (clave === "ranking") {
      const pedida = new URL(u).searchParams.get("respuesta");
      const servida = payloads.ranking?.respuesta?.clave;
      if (pedida !== servida) {
        throw new Error(
          `el cliente ha pedido el ranking de '${pedida}' y el payload de ` +
          `prueba es el de '${servida}': o el cliente elige mal, o el test ` +
          `preparó el payload equivocado`,
        );
      }
    }
    return { ok: true, status: 200, json: async () => vigilar(payloads[clave], clave) };
  },
};

vm.createContext(contexto);
for (const f of ["static/comun.js", "static/graficos.js", "static/metricas.js"]) {
  vm.runInContext(fs.readFileSync(f, "utf8"), contexto, { filename: f });
}

const VISTAS = [
  "portada", "concordancia", "desfase", "impacto", "auditoria", "percepcion",
];

/* Lo que delata una clave mal adivinada que SÍ acaba impresa.
 *
 * `undefined` y `NaN` los escribe JavaScript solo al concatenar; `[object
 * Object]` es un objeto pintado donde iba un número. Los tres se leen desde el
 * móvil como parte de la frase.
 */
const SOSPECHOSOS = ["undefined", "NaN", "[object Object]"];

let fallos = 0;
for (const v of VISTAS) {
  vistaEnCurso = v;
  contexto.location.hash = `#${v}`;
  try {
    await vm.runInContext("cargar()", contexto);
  } catch (e) {
    console.log(`FALLO ${v}: ${e.message}\n${e.stack}`);
    fallos++;
    continue;
  }
  const html = salidas[v] || "";
  // La vista pinta el error en `#cargando` en vez de lanzar, así que un fallo
  // dentro del renderizador saldría como una pantalla "correcta" y vacía.
  if (elementos["cargando"].className.includes("mal")) {
    console.log(`FALLO ${v}: se pintó el error -> ${elementos["cargando"]._html}`);
    fallos++;
    continue;
  }
  if (!html) {
    console.log(`FALLO ${v}: no se pintó nada`);
    fallos++;
    continue;
  }
  const encontrados = SOSPECHOSOS.filter((s) => html.includes(s));
  console.log(
    `${encontrados.length ? "SOSPECHA" : "ok      "} ${v.padEnd(13)} ` +
    `${String(html.length).padStart(7)} bytes` +
    (encontrados.length ? `  -> ${encontrados.join(", ")}` : ""),
  );
  if (encontrados.length) {
    fallos++;
    for (const s of encontrados) {
      const i = html.indexOf(s);
      console.log(`      ...${html.slice(Math.max(0, i - 120), i + 60)}...`);
    }
  }
}

if (inventadas.size) {
  console.log(`\nCLAVES QUE EL PAYLOAD NO TRAE (${inventadas.size}):`);
  for (const k of [...inventadas].sort()) console.log(`  ${k}`);
  fallos++;
}

process.exit(fallos ? 1 : 0);
