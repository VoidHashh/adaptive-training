/*
 * Rellena el formulario del check-in DE VERDAD y enseña lo que sale por el cable.
 *
 * Lo arranca `tests/test_pwa.py`, que le pasa por argumento un fichero con el
 * `/api/checkin/today` que tiene que servir y la lista de toques que hay que
 * dar. Devuelve por stdout, en la última línea, qué se pintó, qué se quedó sin
 * contestar y -lo único que de verdad importa- el CUERPO EXACTO del POST.
 *
 * POR QUÉ ESTE ARNÉS Y NO UNA BÚSQUEDA DE TEXTO
 * ---------------------------------------------
 * Lo que hay que demostrar aquí es que un "No" llega al servidor como `false` y
 * no como `0`, y que una pregunta sin tocar NO llega de ninguna forma. Las dos
 * cosas son conversiones automáticas de JavaScript: `Number(false)` es 0 y
 * `Boolean(0)` es false. Ninguna de las dos da error, ninguna deja rastro en el
 * archivo y las dos producen un check-in perfectamente válido con una respuesta
 * que nadie dio. Buscar `Number(` con una expresión regular no distingue el uso
 * bueno -los siete deslizadores- del malo, y prohibirlo a secas rompería el
 * archivo entero.
 *
 * Así que se pulsa el botón y se mira el JSON. Es la misma decisión que
 * `render_pwa.mjs` y `salud_pwa.mjs`: la única forma de saber si algo se pinta
 * es pintarlo, y la única forma de saber qué se envía es enviarlo.
 *
 * EL DOM DE AQUÍ ABAJO
 * --------------------
 * Es de verdad, dentro de lo tonto que puede ser: parsea el HTML que genera la
 * pantalla en vez de buscar las cadenas que yo sé que escribe. Esa diferencia es
 * la que hace que sirva. Un arnés que busque `data-respuesta="si"` porque lo he
 * escrito yo arriba comprueba que sé lo que escribí; éste se entera si el día de
 * mañana los botones se pintan de otra manera, porque deja de encontrarlos.
 *
 * No hay `jsdom` ni `package.json` en este repositorio a propósito -la batería
 * tiene que correr con `node` a secas-, así que el DOM va aquí.
 */

import fs from "node:fs";
import vm from "node:vm";

const RUTA = process.argv[2];
if (!RUTA) {
  console.error("uso: node tests/checkin_pwa.mjs <guion.json>");
  process.exit(2);
}
const guion = JSON.parse(fs.readFileSync(RUTA, "utf8"));

// ---------------------------------------------------------------------------
// Un DOM pequeño pero honrado
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

// Los elementos con `id` del `index.html`. Se crean a demanda y son las RAÍCES
// desde las que busca `document.querySelector`: lo que se desengancha de una de
// ellas deja de encontrarse, que es lo que pasa en un navegador y lo que hace
// que repintar el formulario no deje fantasmas.
const porId = new Map();
function elemento(id) {
  if (!porId.has(id)) {
    const el = new Elemento("div");
    el.id = id;
    // NACEN COMO NACEN EN EL HTML, no en blanco. Un `<section hidden>` que aquí
    // empezara visible haría que «la tarjeta no está en pantalla» fuese
    // imposible de comprobar: saldría siempre un hueco vacío haciéndose pasar
    // por una tarjeta pintada, y el test que quisiera demostrar que todavía no
    // se ha previsualizado nada estaría mirando un `false` que no significa
    // nada. Lo mismo con `disabled` y el botón de enviar.
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

// Lo que sale por el cable al previsualizar. En lista y no en una variable
// suelta: la segunda previsualización del día es medio encargo -«no quiero que
// la segunda tape a la primera»- y con una sola variable el arnés haría
// exactamente lo que el encargo prohíbe, quedarse con la última.
const cuerposPrevisualizados = [];
let cuerpoDesacuerdo = null;
let urlDesacuerdo = null;

/* Lo que contesta `/api/preview` cuando el guion no dice otra cosa.
 *
 * Copia la forma del endpoint de verdad, incluido lo que más importa: el
 * desglose de `NADA_EJECUTADO` va SUELTO en la raíz -no dentro de `decision`- y
 * no lleva ni `decided` ni `checkin_saved`. Un doble que los trajera dejaría
 * pasar el día que el backend empezara a mandarlos, que es justo la confusión
 * que esta pantalla existe para no tener.
 *
 * `preview_id` y `seq` suben con cada llamada porque la tabla es de añadir: un
 * doble que devolviera siempre `seq: 1` haría imposible ver la revisión, que es
 * media razón de que la tabla exista.
 */
function previsualizacionPorDefecto(n) {
  return {
    day: "2026-09-15",
    ejecutado: false,
    checkin_guardado: false,
    decision_guardada: false,
    hevy: "sin tocar",
    telegram: "sin tocar",
    previsualizacion_guardada: true,
    preview_id: n,
    seq: n,
    revision: n > 1,
    light: "amber",
    trigger_rule: "fatiga_alta",
    decision: {
      light: "amber",
      trigger_rule: "fatiga_alta",
      fired_rules: [
        { name: "fatiga_alta", detail: ["fatigue 7 ≥ 6"] },
        { name: "sueño_corto", detail: ["sleep 5.1 h < 6 h"] },
      ],
      skipped_rules: [
        { name: "dolor_lumbar", missing: ["lower_back_pain"] },
      ],
      notes: ["Ayer fue día de pierna"],
      session: {
        title: "Día 3 · Pierna",
        kind: "reduced",
        changes: ["Sentadilla: 4×5 → 3×5"],
        dropped: ["Peso muerto rumano"],
        notes: ["Sin fallo en ninguna serie"],
      },
      progression: {
        gate_open: false,
        gate_reason: "La progresión está cerrada mientras el día no sea verde",
        changes: [],
      },
      progression_hiit: { changes: [] },
      bike: {
        applies: true,
        label: "Rodaje suave",
        duration_min: 40,
        duration_max: 55,
        detail: "Zona 2, sin series",
        baseline_en_claro: "Punto de partida: 50 min, de tus 4 últimas salidas",
        downgrades: [{ why: "Día ámbar: se recorta un escalón" }],
        notas: ["Si el lumbar molesta, bájate"],
      },
    },
  };
}

// El `localStorage` del móvil. De verdad, no un doble mudo: el borrador es la
// red del envío que no llega, y el guion puede sembrarlo para arrancar como
// arranca una pantalla que se abre por segunda vez.
const almacen = new Map();
if (guion.borrador !== undefined && guion.borrador !== null) {
  almacen.set("checkin-borrador", JSON.stringify(guion.borrador));
}

const contexto = {
  console,
  document: {
    getElementById: (id) => (porId.has(id) || ID_DEL_HTML.has(id) ? elemento(id) : null),
    createElement: (t) => new Elemento(t),
    querySelector: (s) => buscarTodo(s)[0] || null,
    querySelectorAll: buscarTodo,
  },
  // `CSS.escape` de verdad no hace falta entera: las claves del config son
  // `[a-z_]`. Se deja la identidad y se comprueba: si algún día una clave trae
  // un carácter que habría que escapar, esto levanta en vez de construir un
  // selector roto que no encuentra nada y se salta en silencio.
  CSS: {
    escape(s) {
      if (!/^[A-Za-z_][\w-]*$/.test(String(s))) {
        throw new Error(`clave que necesitaría escape de verdad: ${s}`);
      }
      return String(s);
    },
  },
  location: { origin: "http://x", hash: "", pathname: "/" },
  localStorage: {
    getItem: (k) => (almacen.has(k) ? almacen.get(k) : null),
    setItem: (k, v) => almacen.set(k, String(v)),
    removeItem: (k) => almacen.delete(k),
  },
  addEventListener: () => {},
  setTimeout, clearTimeout, setInterval, clearInterval,
  window: { addEventListener: () => {}, location: { pathname: "/" } },
  navigator: {},
  Math, Number, String, Object, Array, JSON, Set, Map, Date, URL, Error,
  Boolean, Proxy, RegExp,
  isNaN, parseInt, parseFloat, encodeURIComponent, Promise,

  async fetch(url, opciones) {
    const u = String(url);
    if (u.includes("/api/checkin/today")) {
      return { ok: true, status: 200, json: async () => guion.hoy };
    }
    if (u.includes("/api/checkin")) {
      vecesEnviado += 1;
      cuerpoEnviado = JSON.parse(opciones.body);
      // `guion.respuesta` deja que el test elija QUÉ contesta el servidor, y no
      // es un lujo: la rama interesante de `pintarResultado` es la de
      // `decided: false`, y con la respuesta clavada a `decided: true` esa rama
      // no la pisaba el arnés nunca. Ahí vivió durante meses una frase fija que
      // afirmaba que no se había enviado ningún mensaje, incluso los días en que
      // sí se había enviado.
      return {
        ok: true,
        status: 200,
        json: async () => guion.respuesta ?? {
          decided: true, light: "green", session: "Día 1",
          hevy: "escrita", telegram: "enviado", problems: [],
        },
      };
    }
    // EL DESACUERDO ANTES QUE LA PREVISUALIZACIÓN, y no es cosmético: su URL
    // es `/api/preview/3/desacuerdo`, que CONTIENE `/api/preview`. Al revés,
    // esta rama no se pisaría nunca, el desacuerdo se contaría como una
    // previsualización más y el test que mira `veces_previsualizado` daría un
    // número de más sin que nada reventara.
    if (u.includes("/api/preview/") && u.includes("/desacuerdo")) {
      urlDesacuerdo = u;
      cuerpoDesacuerdo = JSON.parse(opciones.body);
      const g = guion.desacuerdo || {};
      const status = g.status || 200;
      return {
        ok: status < 400,
        status,
        json: async () => g.respuesta ?? {
          day: "2026-09-15",
          preview_id: Number(/\/api\/preview\/(\d+)\//.exec(u)[1]),
          seq: 1,
          // EL MOTIVO NORMALIZADO, como lo devuelve el servidor de verdad: uno
          // de solo espacios se guarda como nada. El defecto lo imita porque la
          // tarjeta tiene que pintar lo GUARDADO y no lo tecleado, y un doble
          // que devolviera el texto tal cual dejaría esa diferencia sin poder
          // verse.
          disagreed: cuerpoDesacuerdo.disagreed,
          disagreement_reason: (cuerpoDesacuerdo.reason || "").trim() || null,
          ejecutado: false,
          checkin_guardado: false,
          decision_guardada: false,
          hevy: "sin tocar",
          telegram: "sin tocar",
        },
      };
    }
    if (u.includes("/api/preview")) {
      cuerposPrevisualizados.push(JSON.parse(opciones.body));
      const n = cuerposPrevisualizados.length;
      // La enésima previsualización recibe la enésima respuesta del guion, y si
      // se acaban se repite la última. Así un test que solo quiere una tarjeta
      // escribe una, y el que quiere ver la revisión escribe dos.
      const guionadas = guion.previsualizaciones || [];
      const elegida = guionadas[n - 1] ?? guionadas[guionadas.length - 1];
      const status = (elegida && elegida.status) || 200;
      return {
        ok: status < 400,
        status,
        json: async () =>
          (elegida && elegida.respuesta) ?? previsualizacionPorDefecto(n),
      };
    }
    if (u.includes("/api/health")) {
      // Todo en orden: los avisos de salud tienen su propio arnés y aquí solo
      // estorbarían encima del formulario.
      return {
        ok: true, status: 200,
        json: async () => ({
          secrets_missing: [], dry_run: false, writes: {},
          scheduler: { running: true, jobs: { x: {} }, error: null },
          clock: { matches: true }, config_file: { in_sync: true },
        }),
      };
    }
    throw new Error(`fuera de alcance en este arnés: ${u}`);
  },
};
contexto.globalThis = contexto;

// Los `id` que existen en `index.html`, CON el estado con el que nacen. Fuera de
// esta lista, `getElementById` devuelve `null` como en un navegador: si `app.js`
// empieza a pedir un hueco que nadie ha puesto en el HTML, aquí revienta con un
// `null` en vez de inventarse el elemento y dejar el olvido sin consecuencias.
//
// Se leen las etiquetas enteras y no solo `id="..."` porque hace falta saber si
// el elemento viene con `hidden` o con `disabled` puestos a mano. Son atributos
// sin valor -`<section id="x" hidden>`-, así que se buscan como palabra suelta
// dentro de los atributos de SU etiqueta; buscarlos en todo el archivo los
// encontraría en cualquier otra.
const ID_DEL_HTML = new Map(
  [...fs.readFileSync("static/index.html", "utf8")
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

vm.createContext(contexto);
for (const f of ["static/comun.js", "static/app.js"]) {
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

await drenar();

if (elemento("formulario").hidden !== false) {
  throw new Error("`arrancar()` no ha terminado: el formulario sigue oculto");
}

for (const a of guion.acciones || []) {
  if (a.tipo === "responder") {
    const bloque = contexto.document.querySelector(`.pregunta[data-key="${a.key}"]`);
    if (!bloque) throw new Error(`no hay ninguna pregunta pintada con la clave ${a.key}`);
    const boton = bloque.querySelector(`[data-respuesta="${a.respuesta}"]`);
    if (!boton) throw new Error(`la pregunta ${a.key} no tiene botón '${a.respuesta}'`);
    disparar(boton, "click");
  } else if (a.tipo === "deslizar") {
    const fila = contexto.document.querySelector(`.slider[data-key="${a.key}"]`);
    if (!fila) throw new Error(`no hay ningún deslizador pintado con la clave ${a.key}`);
    const input = fila.querySelector("input");
    input.value = String(a.valor);
    disparar(input, "input");
  } else if (a.tipo === "elegir") {
    const bloque = contexto.document.querySelector(`.selector[data-key="${a.key}"]`);
    if (!bloque) throw new Error(`no hay ningún selector pintado con la clave ${a.key}`);
    const boton = bloque.querySelector(`[data-opcion="${a.opcion}"]`);
    if (!boton) throw new Error(`el selector no ofrece la opción '${a.opcion}'`);
    disparar(boton, "click");
  } else if (a.tipo === "enviar") {
    disparar(elemento("formulario"), "submit", { preventDefault() {} });
    await drenar();
  } else if (a.tipo === "previsualizar") {
    // Por el BOTÓN y no llamando a `previsualizar()` dentro del contexto. La
    // función existía y nadie la había enganchado: un arnés que la invocara a
    // mano habría pintado la tarjeta perfectamente en verde mientras en el
    // móvil el botón no hacía absolutamente nada.
    disparar(elemento("previsualizar"), "click");
    await drenar();
  } else if (a.tipo === "pedir-sesion") {
    // Por el BOTÓN del control, igual que `previsualizar`: tocar
    // `estado.sesionPedida` desde fuera probaría que el cuerpo se arma bien a
    // partir del estado, que no es lo que hay que demostrar. Lo que hay que
    // demostrar es que PULSAR una opción acaba cambiando lo que sale por el
    // cable, que es la cadena entera y es donde estaba el agujero.
    // En DOS pasos y no con `.eleccion-sesion [data-sesion=...]`: ese selector
    // lleva un espacio, y este arnés rechaza los descendientes a gritos en vez
    // de devolver `null` para todo. Es la misma trampa que dejó
    // `rangos_en_preguntas` a cero durante toda su vida.
    const bloque = elemento("previsualizacion").querySelector(".eleccion-sesion");
    if (!bloque) throw new Error("la tarjeta no trae el control de qué sesión hacer");
    const sel = a.sesion === null ? '[data-sesion=""]' : `[data-sesion="${a.sesion}"]`;
    const b = bloque.querySelector(sel);
    if (!b) throw new Error(`la tarjeta no ofrece la opción ${a.sesion}`);
    disparar(b, "click");
    await drenar();
  } else if (a.tipo === "motivo-anulacion") {
    const caja = elemento("previsualizacion").querySelector(".texto-anulacion");
    if (!caja) throw new Error("no hay caja del motivo de la anulación");
    caja.value = String(a.texto);
    disparar(caja, "input");
  } else if (a.tipo === "confirmar-rojo") {
    const b = elemento("previsualizacion").querySelector(
      a.respuesta === "si" ? ".confirmar-si" : ".confirmar-no",
    );
    if (!b) throw new Error("no se está preguntando por la subida en rojo");
    disparar(b, "click");
    await drenar();
  } else if (a.tipo === "abrir-desacuerdo") {
    const b = elemento("previsualizacion").querySelector(".abrir-desacuerdo");
    if (!b) throw new Error("la tarjeta no ofrece dónde declarar el desacuerdo");
    disparar(b, "click");
  } else if (a.tipo === "motivo") {
    const caja = elemento("previsualizacion").querySelector(".texto-desacuerdo");
    if (!caja) throw new Error("no hay caja del motivo: ¿se abrió el desacuerdo?");
    caja.value = String(a.texto);
  } else if (a.tipo === "guardar-desacuerdo") {
    const b = elemento("previsualizacion").querySelector(".guardar-desacuerdo");
    if (!b) throw new Error("no hay botón de guardar el desacuerdo");
    disparar(b, "click");
    await drenar();
  } else {
    throw new Error(`acción desconocida: ${a.tipo}`);
  }
}

// ---------------------------------------------------------------------------
// Lo que se ha quedado en pantalla
// ---------------------------------------------------------------------------

/* Qué botón está marcado, LEÍDO DEL DOM y no de `estado.valores`.
 *
 * Es la diferencia entre comprobar que el dato se guardó y comprobar que se ve.
 * Las dos hacen falta y son distintas: un `estado.valores.will_train === false`
 * con los dos botones en blanco es un formulario que ha decidido por su cuenta. */
function elegidas() {
  const fuera = {};
  for (const bloque of buscarTodo(".pregunta")) {
    const marcados = bloque
      .querySelectorAll("[data-respuesta]")
      .filter((b) => b.classList.contains("elegida"))
      .map((b) => b.dataset.respuesta);
    if (marcados.length > 1) {
      throw new Error(`${bloque.dataset.key} tiene dos respuestas marcadas a la vez`);
    }
    fuera[bloque.dataset.key] = marcados[0] || null;
  }
  return fuera;
}

/* El selector, leído del DOM y con las DOS marcas por separado.
 *
 * `elegida` es la que ha pulsado un dedo y `propuesta` la que el servidor dice
 * que toca. Se informan aparte porque la única forma de demostrar que no se
 * confunden es poder mirarlas una al lado de la otra: una `propuesta` que
 * también saliera como `elegida` sería el formulario declarando por su cuenta,
 * y en el DOM eso son dos clases distintas sobre el mismo botón.
 */
function seleccion() {
  const bloque = buscarTodo(".selector")[0];
  if (!bloque) return null;
  const botones = bloque.querySelectorAll("[data-opcion]");
  const marcadas = botones
    .filter((b) => b.classList.contains("elegida"))
    .map((b) => b.dataset.opcion);
  if (marcadas.length > 1) {
    throw new Error(`el selector tiene ${marcadas.length} opciones elegidas a la vez`);
  }
  return {
    key: bloque.dataset.key,
    opciones: botones.map((b) => b.dataset.opcion),
    etiquetas: botones.map((b) => b.querySelector(".titulo").textContent),
    elegida: marcadas[0] || null,
    propuesta:
      botones.filter((b) => b.classList.contains("propuesta"))
             .map((b) => b.dataset.opcion)[0] || null,
    // Y con qué PALABRAS se dice. `propuesta` de ahí arriba es una clase, y una
    // clase solo la lee una hoja de estilos: si mañana desapareciera el texto,
    // `propuesta` seguiría informando de una marca que en pantalla no dice nada.
    // Quitar el rótulo dejaba la suite verde hasta que esto existió.
    toca: Object.fromEntries(
      botones
        .filter((b) => b.querySelector(".toca"))
        .map((b) => [b.dataset.opcion, b.querySelector(".toca").textContent]),
    ),
    // Las opciones que llevan el apunte de lo que llevan paradas, y el texto
    // entero de cada una: lo que se ve al ir a elegir.
    parados: Object.fromEntries(
      botones
        .filter((b) => b.querySelector(".parado"))
        .map((b) => [b.dataset.opcion, b.querySelector(".parado").textContent]),
    ),
    sin_contestar: bloque.classList.contains("sin-contestar"),
    aria: Object.fromEntries(
      botones.map((b) => [b.dataset.opcion, b.getAttribute("aria-pressed")]),
    ),
  };
}

const faltan = elemento("faltan");

console.log(JSON.stringify({
  // Las claves de las preguntas pintadas, EN ORDEN.
  preguntas: buscarTodo(".pregunta").map((p) => p.dataset.key),
  deslizadores: buscarTodo(".slider").map((s) => s.dataset.key),
  html_preguntas: elemento("preguntas").innerHTML,
  sin_contestar: buscarTodo(".pregunta.sin-contestar").map((p) => p.dataset.key),
  elegidas: elegidas(),
  selector: seleccion(),
  // Un `<input type=range>` dentro de una pregunta -o del selector- sería el
  // fallo de haberlos metido por el camino de los deslizadores.
  //
  // En dos pasos y no con `.pregunta input[...]`: ese selector lleva un espacio,
  // y ahora `casa` lo rechaza a gritos en vez de devolver `false` para todo. Lo
  // hacía, y por eso este número fue 0 durante toda la vida del arnés.
  rangos_en_preguntas: buscarTodo(".pregunta")
    .concat(buscarTodo(".selector"))
    .reduce((n, b) => n + b.querySelectorAll('input[type="range"]').length, 0),
  // EL ARNÉS MIRÁNDOSE A SÍ MISMO, y sí, es un test de un test: el aviso de
  // `casa` es lo único que separa "no hay ninguno" de "no sé buscarlo", y
  // mientras no reventaba de verdad se comió una línea entera de la suite
  // durante toda su vida sin que nada se pusiera rojo. Un guardia que protege
  // contra assertions muertas no puede ser él mismo una assertion muerta, así
  // que sale por el mismo cable que todo lo demás y hay un test que lo mira.
  arnes_rechaza_descendientes: (() => {
    try {
      casa(elemento("preguntas"), '.pregunta input[type="range"]');
      return false;
    } catch {
      return true;
    }
  })(),
  aria: Object.fromEntries(
    buscarTodo("[data-respuesta]").map((b) => [
      `${b.padre.padre.dataset.key}.${b.dataset.respuesta}`,
      b.getAttribute("aria-pressed"),
    ]),
  ),
  faltan: faltan.hidden ? null : faltan.textContent,
  // La tarjeta del final, tal cual queda. Sale el HTML y no el texto porque lo
  // que se quiere mirar incluye el `<dl>` de Hevy y Telegram.
  resultado: elemento("resultado").hidden
    ? null
    : {
        clase: elemento("resultado").className,
        html: elemento("resultado").innerHTML,
      },
  enviar_deshabilitado: elemento("enviar").disabled,
  // La tarjeta de mirar, en su propio hueco. Sale el HTML entero porque lo que
  // hay que poder comprobar es la cabecera de qué se ha tocado, que es un `<dl>`.
  previsualizacion: elemento("previsualizacion").hidden
    ? null
    : {
        clase: elemento("previsualizacion").className,
        html: elemento("previsualizacion").innerHTML,
        texto: elemento("previsualizacion").textContent,
      },
  // Que el botón sea `type="button"` NO sale por aquí, y no es un olvido: este
  // arnés fabrica un `<div>` por cada `id` y no conoce la etiqueta de verdad.
  // Ese es un hecho del HTML y se comprueba leyendo el HTML.
  previsualizar_deshabilitado: elemento("previsualizar").disabled,
  valores: vm.runInContext("JSON.stringify(estado.valores)", contexto),
  cuerpo: cuerpoEnviado,
  veces_enviado: vecesEnviado,
  // Todos los cuerpos previsualizados, en orden. Que la segunda no tape a la
  // primera vale también aquí.
  cuerpos_previsualizados: cuerposPrevisualizados,
  veces_previsualizado: cuerposPrevisualizados.length,
  // El texto del bloque del desacuerdo POR SEPARADO. Lo pedía una guarda que
  // buscaba «Queda apuntado» en la tarjeta ENTERA para comprobar que un
  // desacuerdo que no se guarda no dice que sí: al añadir el control de qué
  // sesión hacer, su texto de ayuda usó esa misma frase para hablar de otra
  // cosa y la guarda pasó a fallar siempre. Una guarda que lee toda la
  // pantalla buscando una frase de un bloque se rompe con cualquier texto que
  // se escriba en cualquier otro sitio.
  desacuerdo_texto: (() => {
    const b = elemento("previsualizacion").querySelector(".desacuerdo");
    return b ? b.textContent : null;
  })(),
  cuerpo_desacuerdo: cuerpoDesacuerdo,
  url_desacuerdo: urlDesacuerdo,
  borrador: almacen.has("checkin-borrador")
    ? JSON.parse(almacen.get("checkin-borrador"))
    : null,
}));
