/*
 * Rellena el formulario de DESPUÉS de entrenar de verdad y enseña qué sale.
 *
 * Lo arranca `tests/test_pwa.py` con un guion: qué contesta el servidor a
 * `/api/sesion/hoy`, qué contesta al POST, y la lista de toques. Imprime en la
 * última línea qué se pintó y -lo que de verdad importa- el CUERPO EXACTO del
 * POST.
 *
 * LO QUE HAY QUE DEMOSTRAR ES LO MISMO QUE EN EL CHECK-IN: que una escala sin
 * tocar NO viaja. El `value="5"` del deslizador es dónde se dibuja el pulgar,
 * no una respuesta, y la conversión que lo mandaría como un cinco que nadie
 * contestó no da error ni deja rastro en el archivo. Solo se ve pulsando y
 * mirando el JSON.
 *
 * EL DOM ES UNA COPIA DEL DE `checkin_pwa.mjs`, y lo es a propósito: cada
 * arnés de este repositorio monta el suyo -está en `CLAUDE.md`-, porque no hay
 * `jsdom` ni `package.json` y no los va a haber. Que haya dos copias es una
 * deuda conocida; sacarlas a un módulo común tocaría el arnés del check-in, y
 * eso es un cambio aparte con su propia batería.
 */

import fs from "node:fs";
import vm from "node:vm";

const RUTA = process.argv[2];
if (!RUTA) {
  console.error("uso: node tests/despues_pwa.mjs <guion.json>");
  process.exit(2);
}
const guion = JSON.parse(fs.readFileSync(RUTA, "utf8"));

// ---------------------------------------------------------------------------
// Un DOM pequeño pero honrado (copiado de checkin_pwa.mjs)
// ---------------------------------------------------------------------------

const VACIOS = new Set(["input", "br", "img", "meta", "link", "hr", "source"]);

class Elemento {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = new Map();
    this.hijos = [];           // Elemento o {texto}
    this.padre = null;
    this.oyentes = new Map();
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    const el = this;

    this.classList = {
      add(...cs) { el._clases(new Set([...el._clases(), ...cs])); },
      remove(...cs) {
        const s = el._clases();
        for (const c of cs) s.delete(c);
        el._clases(s);
      },
      contains: (c) => el._clases().has(c),
      toggle(c, forzar) {
        const puesto = forzar === undefined ? !el._clases().has(c) : !!forzar;
        if (puesto) this.add(c); else this.remove(c);
        return puesto;
      },
    };

    // `dataset` de verdad: lee y escribe los atributos `data-*`. Escrito como
    // Proxy y no como objeto suelto porque el código de la pantalla hace las
    // dos cosas -`fila.dataset.key = s.key` al crear, y `boton.dataset.respuesta`
    // al leer un elemento que salió de parsear HTML-, y con un objeto aparte la
    // segunda daría `undefined` sin que nada lo dijera.
    this.dataset = new Proxy({}, {
      get: (_, k) => el.attrs.get(`data-${guion_kebab(k)}`),
      set: (_, k, v) => { el.attrs.set(`data-${guion_kebab(k)}`, String(v)); return true; },
      has: (_, k) => el.attrs.has(`data-${guion_kebab(k)}`),
    });
  }

  _clases(nuevas) {
    if (nuevas === undefined) {
      return new Set((this.attrs.get("class") || "").split(/\s+/).filter(Boolean));
    }
    this.attrs.set("class", [...nuevas].join(" "));
    return nuevas;
  }

  get className() { return this.attrs.get("class") || ""; }
  set className(v) { this.attrs.set("class", String(v)); }

  get id() { return this.attrs.get("id") || ""; }
  set id(v) { this.attrs.set("id", String(v)); }

  setAttribute(n, v) { this.attrs.set(n, String(v)); }
  getAttribute(n) { return this.attrs.has(n) ? this.attrs.get(n) : null; }
  hasAttribute(n) { return this.attrs.has(n); }

  get textContent() {
    return this.hijos
      .map((h) => (h instanceof Elemento ? h.textContent : h.texto))
      .join("");
  }
  set textContent(v) { this.hijos = [{ texto: String(v) }]; }

  get innerHTML() { return this.hijos.map(serializar).join(""); }
  set innerHTML(v) {
    this.hijos = [];
    for (const n of parsear(String(v))) this.appendChild(n);
  }

  appendChild(n) {
    if (n instanceof Elemento) n.padre = this;
    this.hijos.push(n);
    return n;
  }
  prepend(n) {
    if (n instanceof Elemento) n.padre = this;
    this.hijos.unshift(n);
    return n;
  }

  addEventListener(tipo, fn) {
    if (!this.oyentes.has(tipo)) this.oyentes.set(tipo, []);
    this.oyentes.get(tipo).push(fn);
  }

  scrollIntoView() {}

  *descendientes() {
    for (const h of this.hijos) {
      if (h instanceof Elemento) {
        yield h;
        yield* h.descendientes();
      }
    }
  }

  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    return [...this.descendientes()].filter((e) => casa(e, sel));
  }
}

function guion_kebab(k) {
  return String(k).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

const ENTIDADES = { amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" };
function desescapar(s) {
  return s.replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ENTIDADES[e]);
}
function escapar_texto(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function serializar(n) {
  if (!(n instanceof Elemento)) return escapar_texto(n.texto);
  const t = n.tagName.toLowerCase();
  const atrs = [...n.attrs].map(([k, v]) => ` ${k}="${v}"`).join("");
  if (VACIOS.has(t)) return `<${t}${atrs}>`;
  return `<${t}${atrs}>${n.hijos.map(serializar).join("")}</${t}>`;
}

/* Un parser de HTML de andar por casa: etiquetas, atributos entrecomillados y
 * texto. Suficiente para lo que estas plantillas escriben, y ruidoso con lo que
 * no entiende -una etiqueta sin cerrar levanta- para no tragarse en silencio un
 * HTML mal formado, que es justo uno de los fallos que podría estar buscando. */
function parsear(html) {
  const raiz = new Elemento("#fragmento");
  const pila = [raiz];
  let i = 0;

  const texto = (t) => {
    if (t.trim() === "") return;
    pila[pila.length - 1].appendChild({ texto: desescapar(t) });
  };

  while (i < html.length) {
    const lt = html.indexOf("<", i);
    if (lt === -1) { texto(html.slice(i)); break; }
    if (lt > i) texto(html.slice(i, lt));

    const gt = html.indexOf(">", lt);
    if (gt === -1) throw new Error(`etiqueta sin cerrar: ${html.slice(lt, lt + 60)}`);
    let cuerpo = html.slice(lt + 1, gt);
    i = gt + 1;

    if (cuerpo.startsWith("!")) continue;          // comentario o doctype
    if (cuerpo.startsWith("/")) {
      if (pila.length === 1) throw new Error(`cierre de más: </${cuerpo.slice(1)}>`);
      pila.pop();
      continue;
    }

    const auto = cuerpo.endsWith("/");
    if (auto) cuerpo = cuerpo.slice(0, -1);

    const m = /^([A-Za-z][\w-]*)/.exec(cuerpo);
    if (!m) throw new Error(`etiqueta ilegible: <${cuerpo}>`);
    const el = new Elemento(m[1]);

    const re = /([A-Za-z_:][\w:.-]*)(?:\s*=\s*"([^"]*)")?/g;
    re.lastIndex = m[1].length;
    let a;
    while ((a = re.exec(cuerpo)) !== null) {
      el.attrs.set(a[1], a[2] === undefined ? "" : desescapar(a[2]));
    }

    pila[pila.length - 1].appendChild(el);
    if (!auto && !VACIOS.has(m[1].toLowerCase())) pila.push(el);
  }

  if (pila.length !== 1) {
    throw new Error(`quedan ${pila.length - 1} etiquetas sin cerrar en el HTML`);
  }
  return raiz.hijos;
}

/* Selectores simples sobre UN elemento: `tag`, `.clase`, `[attr]`,
 * `[attr="valor"]` y cualquier combinación pegada. No hay descendientes ni
 * comas porque `app.js` no los usa; si algún día los usa, esto revienta en vez
 * de devolver la lista vacía, que es lo que haría pasar un test en verde.
 *
 * Y ESO NO SE CUMPLÍA. Decía "revienta" y no reventaba: con
 * `.pregunta input[type="range"]` consumía `.pregunta`, se saltaba el espacio,
 * leía `[type="range"]` como si fuera del MISMO elemento -que no tiene ese
 * atributo- y devolvía `false` sin llegar nunca a la parte que no entiende. El
 * resultado era el peor posible: `rangos_en_preguntas` daba 0 SIEMPRE, con lo
 * que la línea que comprobaba que las preguntas no se pintan como deslizadores
 * llevaba desde el primer día sin poder ponerse roja.
 *
 * Lo miro ahora porque el selector nuevo tiene la misma forma -un bloque con
 * botones dentro- y se habría escrito igual.
 */
function casa(el, sel) {
  const re = /^([A-Za-z][\w-]*)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/;
  let resto = sel.trim();
  if (!resto) return false;
  // Antes de mirar nada: un descendiente o una coma se cantan aquí. Después ya
  // es tarde, porque el primer trozo que no case devuelve `false` y la lista
  // vacía se lee igual que "no hay ninguno".
  if (/[\s,>+~]/.test(resto)) {
    throw new Error(
      `selector con descendientes o comas, que este arnés no sabe resolver: ` +
      `${sel}. Recorre los bloques y busca dentro de cada uno.`,
    );
  }
  while (resto) {
    const m = re.exec(resto);
    if (!m) throw new Error(`selector que este arnés no entiende: ${sel}`);
    if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
    if (m[2] && !el.classList.contains(m[2])) return false;
    if (m[3]) {
      if (!el.attrs.has(m[3])) return false;
      if (m[4] !== undefined && el.attrs.get(m[3]) !== m[4]) return false;
    }
    resto = resto.slice(m[0].length);
  }
  return true;
}


// Los `id` que existen en `despues.html`, con el estado con el que nacen.
// Fuera de esta lista `getElementById` devuelve `null`: si la pantalla empieza a
// pedir un hueco que nadie ha puesto en el HTML, aquí revienta.
const ID_DEL_HTML = new Map(
  [...fs.readFileSync("static/despues.html", "utf8")
      .matchAll(/<([A-Za-z][\w-]*)((?:\s+[\w:-]+(?:\s*=\s*"[^"]*")?)*)\s*\/?>/g)]
    .map((m) => m[2])
    .filter((atributos) => /\bid\s*=\s*"/.test(atributos))
    .map((atributos) => [
      /\bid\s*=\s*"([^"]*)"/.exec(atributos)[1],
      {
        hidden: /(^|\s)hidden(\s|$)/.test(atributos),
        disabled: /(^|\s)disabled(\s|$)/.test(atributos),
      },
    ]),
);

const porId = new Map();
function elemento(id) {
  if (!porId.has(id)) {
    const el = new Elemento("div");
    el.id = id;
    const nace = ID_DEL_HTML.get(id);
    if (nace) {
      el.hidden = nace.hidden;
      el.disabled = nace.disabled;
    }
    porId.set(id, el);
  }
  return porId.get(id);
}

function buscarTodo(sel) {
  const fuera = [];
  for (const raiz of porId.values()) {
    if (casa(raiz, sel)) fuera.push(raiz);
    for (const d of raiz.descendientes()) if (casa(d, sel)) fuera.push(d);
  }
  return fuera;
}

// ---------------------------------------------------------------------------
// El contexto
// ---------------------------------------------------------------------------

let cuerpoEnviado = null;
let vecesEnviado = 0;
let vecesLeido = 0;

const contexto = {
  console,
  document: {
    getElementById: (id) => (porId.has(id) || ID_DEL_HTML.has(id) ? elemento(id) : null),
    createElement: (t) => new Elemento(t),
    querySelector: (s) => buscarTodo(s)[0] || null,
    querySelectorAll: buscarTodo,
  },
  location: { origin: "http://x", hash: "", pathname: "/despues.html" },
  addEventListener: () => {},
  setTimeout, clearTimeout,
  window: { addEventListener: () => {}, location: { pathname: "/despues.html" } },
  navigator: {},
  Math, Number, String, Object, Array, JSON, Set, Map, Date, URL, Error,
  Boolean, Proxy, RegExp,
  isNaN, parseInt, parseFloat, encodeURIComponent, Promise,

  async fetch(url, opciones) {
    const u = String(url);
    if (u.includes("/api/sesion/hoy")) {
      vecesLeido++;
      if (guion.sin_red) throw new Error("sin red");
      const h = guion.hoy || {};
      return {
        ok: (h.status || 200) < 400,
        status: h.status || 200,
        json: async () => h.json,
      };
    }
    if (u.includes("/api/sesion/feedback")) {
      vecesEnviado++;
      cuerpoEnviado = JSON.parse(opciones.body);
      const p = guion.post || {};
      return {
        ok: (p.status || 200) < 400,
        status: p.status || 200,
        json: async () => p.json || { guardado: true },
      };
    }
    throw new Error(`fetch no previsto en el arnés: ${u}`);
  },
};

vm.createContext(contexto);
for (const f of ["static/comun.js", "static/despues.js"]) {
  vm.runInContext(fs.readFileSync(f, "utf8"), contexto, { filename: f });
}

// ---------------------------------------------------------------------------
// Los toques
// ---------------------------------------------------------------------------

async function drenar() {
  for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r));
}

function disparar(el, tipo, ev = {}) {
  const fns = el.oyentes.get(tipo) || [];
  if (!fns.length) throw new Error(`nadie escucha '${tipo}' en ${el.tagName}#${el.id}`);
  for (const fn of fns) fn(ev);
}

function bloqueDe(contenedor, clase, key) {
  const b = elemento(contenedor).querySelectorAll(`.${clase}`)
    .find((x) => x.dataset.key === key);
  if (!b) throw new Error(`no encuentro .${clase} de '${key}' en #${contenedor}`);
  return b;
}

for (const a of guion.acciones || []) {
  if (a.cargar) {
    disparar(elemento("cargar-sesion"), "click");
    await drenar();
  } else if (a.escala) {
    const fila = bloqueDe("escalas-despues", "slider", a.escala);
    const input = fila.querySelector("input");
    input.value = String(a.valor);
    disparar(input, "input");
  } else if (a.eleccion) {
    const bloque = bloqueDe("elecciones-despues", "pregunta", a.eleccion);
    const b = bloque.querySelectorAll("[data-valor]").find((x) => x.dataset.valor === a.valor);
    if (!b) throw new Error(`no hay botón '${a.valor}' en ${a.eleccion}`);
    disparar(b, "click");
  } else if (a.respuesta) {
    const s = [...elemento("faltan-despues").querySelectorAll(".respuesta-ejercicio"),
               ...elemento("hechos-despues").querySelectorAll(".respuesta-ejercicio")]
      .find((x) => x.dataset.key === a.respuesta);
    if (!s) throw new Error(`no hay desplegable para '${a.respuesta}'`);
    s.value = a.valor;
    disparar(s, "change");
  } else if (a.costoso !== undefined) {
    const s = elemento("costoso-despues").querySelector("select");
    if (!s) throw new Error("no hay desplegable de «¿cuál te costó más?»");
    s.value = a.costoso;
    disparar(s, "change");
  } else if (a.nota !== undefined) {
    elemento("nota-despues").value = a.nota;
  } else if (a.guardar) {
    disparar(elemento("form-despues"), "submit", { preventDefault() {} });
    await drenar();
  } else {
    throw new Error(`acción desconocida: ${JSON.stringify(a)}`);
  }
}

// ---------------------------------------------------------------------------
// Lo que se ve
// ---------------------------------------------------------------------------

const escalas = elemento("escalas-despues").querySelectorAll(".slider").map((f) => ({
  key: f.dataset.key,
  sin_contestar: f.classList.contains("sin-contestar"),
  manana: (f.querySelector(".manana") || { textContent: null }).textContent,
  valor: f.querySelector(".valor").textContent,
}));
const faltan = elemento("faltan-despues").querySelectorAll(".nombre-ejercicio")
  .map((n) => n.textContent);
const hechos = elemento("hechos-despues").querySelector(".bloque-hechos");
const aviso = elemento("aviso-despues");

console.log(JSON.stringify({
  form_visible: elemento("form-despues").hidden === false,
  aviso: aviso.hidden ? null : aviso.textContent,
  resumen: elemento("resumen-sesion").textContent,
  faltan,
  hechos_titulo: hechos ? hechos.querySelector("summary").textContent : null,
  hechos_abierto: hechos ? hechos.hasAttribute("open") : null,
  escalas,
  guardado: elemento("guardado-despues").hidden === false,
  veces_leido: vecesLeido,
  veces_enviado: vecesEnviado,
  cuerpo: cuerpoEnviado,
}));
