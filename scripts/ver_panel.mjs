/*
 * El mismo andamio que `tests/render_pwa.mjs`, pero para MIRAR en vez de para
 * afirmar: coge los payloads de la base de verdad y escribe el HTML de cada
 * vista en un fichero, envuelto en la página real y con la hoja de estilos de
 * verdad, para poder abrirlo en un navegador.
 *
 * Lo arranca `scripts/ver_panel.py`. No comprueba nada y no falla por nada: la
 * comprobación es el test, esto es la ventana.
 */

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const [, , RUTA_PAYLOADS, DESTINO] = process.argv;
if (!RUTA_PAYLOADS || !DESTINO) {
  console.error("uso: node scripts/ver_panel.mjs <payloads.json> <carpeta>");
  process.exit(2);
}
const payloads = JSON.parse(fs.readFileSync(RUTA_PAYLOADS, "utf8"));

const salidas = {};
const elementos = {};
function elemento(id) {
  if (!elementos[id]) {
    elementos[id] = {
      id, _html: "", textContent: "", className: "", hidden: false,
      value: id === "dias" ? "180" : "",
      options: [{ value: "180" }],
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = v; if (id === "vista") salidas[vistaEnCurso] = v; },
      addEventListener() {}, appendChild() {}, prepend() {},
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
    const clave = Object.entries(RUTAS)
      .filter(([, r]) => u.includes(r))
      .sort((a, b) => b[1].length - a[1].length)[0]?.[0];
    if (!clave) throw new Error(`ruta no prevista: ${u}`);
    return { ok: true, status: 200, json: async () => payloads[clave] };
  },
};

vm.createContext(contexto);
for (const f of ["static/comun.js", "static/graficos.js", "static/metricas.js"]) {
  vm.runInContext(fs.readFileSync(f, "utf8"), contexto, { filename: f });
}

const VISTAS = [
  "portada", "concordancia", "desfase", "impacto", "auditoria", "percepcion",
];

const css = fs.readFileSync("static/styles.css", "utf8");

for (const v of VISTAS) {
  vistaEnCurso = v;
  contexto.location.hash = `#${v}`;
  try {
    await vm.runInContext("cargar()", contexto);
  } catch (e) {
    console.log(`FALLO ${v}: ${e.message}`);
    continue;
  }
  const html = salidas[v] || "";
  const titulo = elementos["titulo"].textContent;
  const sub = elementos["subtitulo"].textContent;
  const pagina =
    `<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">` +
    `<meta name="viewport" content="width=device-width, initial-scale=1">` +
    `<title>${titulo}</title><style>${css}</style></head>` +
    `<body class="con-nav"><main id="app">` +
    `<header class="cab"><h1>${titulo}</h1><p class="fecha">${sub}</p></header>` +
    `<section id="vista">${html}</section></main></body></html>`;
  const destino = path.join(DESTINO, `${v}.html`);
  fs.writeFileSync(destino, pagina, "utf8");
  console.log(
    `${v.padEnd(13)} ${String(html.length).padStart(7)} bytes  ` +
    `"${titulo}" / "${sub}"`,
  );
}
