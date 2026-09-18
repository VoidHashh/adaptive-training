/*
 * Pinta las ocho vistas de la PWA en Node, contra payloads de verdad, y busca
 * lo que un renderizador hace cuando lee una clave que el backend no manda.
 *
 * Lo arranca `tests/test_pwa.py::test_los_renderizadores_no_leen_ni_una_clave_
 * que_el_backend_no_mande`, que antes le escribe las nueve respuestas de
 * `/api/metrics/*` en el JSON que se le pasa por argumento. Aquí no se toca la
 * base ni la red: entra un JSON, salen las ocho pantallas.
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
  umbral: "/api/metrics/umbral",
  calibracion: "/api/metrics/calibracion",
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
  "umbral", "calibracion",
];

/* Lo que delata una clave mal adivinada que SÍ acaba impresa.
 *
 * `undefined` y `NaN` los escribe JavaScript solo al concatenar; `[object
 * Object]` es un objeto pintado donde iba un número. Los tres se leen desde el
 * móvil como parte de la frase.
 */
const SOSPECHOSOS = ["undefined", "NaN", "[object Object]"];

/* Lo que cada vista TIENE que haber pintado.
 *
 * El `Proxy` de arriba caza una clave LEÍDA que no existe. No caza la contraria
 * -una clave que el backend manda y el renderizador tira a la basura-, y esa
 * también ha pasado aquí: la API llevaba un commit entero mandando `tabla` con
 * las cuatro casillas y `metricas.js` no la miraba. La pantalla se veía entera,
 * sin un `undefined`, sin una rama al revés, y le faltaba exactamente lo que se
 * había pedido. Ni este arnés ni ningún otro test se movieron.
 *
 * No es la lista de todo lo que se pinta: eso sería una captura de pantalla, y
 * se rompería cada vez que alguien cambiara una coma. Es la lista corta de lo
 * que, si desaparece, desaparece callando.
 */
const EXIGIDOS = {
  // `class="casillas"` es el contenedor de la tabla de las dos preguntas;
  // `discorde` es la clase de las filas donde apetecer y entrenar no
  // coincidieron, que solo se pinta por la rama con datos. Los dos juntos
  // distinguen "la tabla está" de "la tabla dice que no hay bastante".
  //
  // Aquí van solo las marcas de ESTRUCTURA, las que no viajan en el payload.
  // El texto de la tabla lo comprueba `tablaMalPintada`, comparándolo contra lo
  // que mandó el servidor en vez de contra una frase escrita aquí.
  portada: ['class="casillas"', "discorde"],
  concordancia: ['class="casillas"', "discorde"],
};

/* La tabla de las dos preguntas, comparada con lo que el servidor mandó.
 *
 * `EXIGIDOS` sabe si una palabra está o no está. Esto sabe DÓNDE está, y hace
 * falta por dos motivos que la batería de mutaciones enseñó en verde:
 *
 *   - quitar la ficha -«93 días con las dos contestadas · 27 con solo una»- no
 *     ponía nada rojo, y es justo el denominador que impide leer el 30 % como
 *     un 30 % de todos los días;
 *   - reordenar las cuatro casillas en el navegador tampoco, y una tabla de dos
 *     por dos que se reordena sola deja de poderse comparar consigo misma de un
 *     mes para otro: la casilla de arriba cambia de significado sin que cambie
 *     ni un número. El comentario de `tablaDiscordancia` lo prohíbe y no lo
 *     ataba nadie.
 *
 * Se compara contra el PAYLOAD y no contra una lista escrita aquí. Lo que se
 * vigila es que el navegador no toque lo que le dan, no cuál es el texto: así
 * reescribir una etiqueta en `preguntas.py` no obliga a tocar este archivo, y
 * en cambio pintarla en otro sitio sí se ve.
 */
function tablasDelPayload(clave) {
  const p = payloads[clave];
  if (!p) return [];
  const lineas = clave === "portada" ? (p.como_voy || {}).lineas : p.series;
  return (lineas || []).map((l) => l.tabla).filter(Boolean);
}

/* El MISMO `escapar` que usa el renderizador, sacado del contexto.
 *
 * Reescribirlo aquí sería tener dos versiones de la regla de escapado, y la
 * copia de este archivo solo se usaría para comparar: el día que a la ficha le
 * entrara un `&` o unas comillas, la comparación fallaría y el mensaje diría
 * "el servidor mandó `ficha` y no está en la pantalla" estando. */
const escapar = vm.runInContext("escapar", contexto);

function tablaMalPintada(clave, html) {
  for (const t of tablasDelPayload(clave)) {
    if (t.na) continue;
    for (const [campo, texto] of [["ficha", t.ficha], ["lectura", t.lectura]]) {
      if (texto && !html.includes(escapar(texto))) {
        return `el servidor mandó \`${campo}\` y no está en la pantalla: "${texto}"`;
      }
    }
    const donde = t.celdas.map((c) => html.indexOf(escapar(c.etiqueta)));
    const perdida = t.celdas.find((c, i) => donde[i] < 0);
    if (perdida) return `la casilla "${perdida.etiqueta}" no se pinta`;
    for (let i = 1; i < donde.length; i++) {
      if (donde[i] < donde[i - 1]) {
        return (
          `las casillas salen en otro orden que el del payload: ` +
          `"${t.celdas[i].etiqueta}" antes que "${t.celdas[i - 1].etiqueta}"`
        );
      }
    }
  }
  return null;
}

/* LA CORRECCIÓN, EN EL PODIO, PUESTO POR PUESTO.
 *
 * `corregir_tanda` corrige la tanda entera en el servidor y manda `p_corregida`
 * y `significativa` en cada casilla del ranking. El navegador las tiraba: la
 * sección se pintaba con `ficha()`, el resumen compartido de `comun.js`, que no
 * lleva ninguna de las dos. Ciento catorce veredictos calculados, servidos y no
 * leídos, en la ÚNICA pantalla que ordena correlaciones en un podio numerado.
 * Medido contra la HRV el 2026-09-17: de 66 calculadas no aguantaba ninguna, y
 * el primer puesto enseñaba «r = −0,38» en negrita sin una palabra al lado.
 *
 * Es la contraria de la que caza el `Proxy`: una clave que el backend SÍ manda y
 * el renderizador no mira. No deja `undefined`, no deja una rama al revés, no
 * deja un hueco. Deja una pantalla entera que afirma de más.
 *
 * Se compara contra el PAYLOAD, con el mismo `num` del renderizador, y no contra
 * un número escrito aquí: así cambiar el redondeo o la ventana no obliga a tocar
 * este archivo, y en cambio dejar de pintar el veredicto sí se ve.
 */
function correccionNoPintada(html) {
  const rank = payloads.ranking;
  if (!rank || !(rank.ranking || []).length) return null;
  const num = vm.runInContext("num", contexto);
  const k = rank.retardos[0];
  for (const e of rank.ranking) {
    const c = (e.por_dia || []).find((x) => x.dias_despues === k);
    if (!c || c.r === null || c.r === undefined) continue;
    if (c.p_corregida !== null && c.p_corregida !== undefined) {
      if (!html.includes(num(c.p_corregida, 3))) {
        return (
          `el servidor mandó \`p_corregida\` = ${num(c.p_corregida, 3)} para ` +
          `"${e.etiqueta || e.clave}" y no está en la pantalla`
        );
      }
    }
    if (typeof c.significativa === "boolean") {
      // Con el separador delante a propósito: "aguanta la corrección" es un
      // trozo de "no aguanta la corrección", así que buscarlo pelado daría por
      // bueno justo el veredicto del revés, que es el peor fallo posible aquí.
      const frase = c.significativa
        ? "· aguanta la corrección"
        : "· no aguanta la corrección";
      if (!html.includes(frase)) {
        return (
          `el servidor dijo \`significativa\` = ${c.significativa} para ` +
          `"${e.etiqueta || e.clave}" y la pantalla no escribe "${frase}"`
        );
      }
    }
  }
  return null;
}


/* LA REGLA 2, MECANIZADA.
 *
 * Literal, del encargo del 2026-09-15 y repetida el 17: «Nada de p corregida,
 * correlación, percentiles ni mitad central en la vista principal. Todo eso
 * detrás de "ver detalle"».
 *
 * Esta guarda existe porque la regla ya se perdió una vez. Se dio por
 * cumplida mirando pantallas a ojo, y a ojo una pantalla siempre parece
 * razonable: cada número tiene su motivo, cada ficha explica algo, y ninguno
 * se ve de más hasta que se cuentan todos juntos. Contados, la vista de
 * impacto traía setenta y un «p corregida» y tres mil seiscientas palabras
 * antes de tocar nada.
 *
 * QUÉ SE MIRA, EXACTAMENTE
 * Lo que se ve al abrir la pantalla en el móvil sin tocar nada: el HTML menos
 * lo que hay dentro de un `<details>`. Se recorta el `<details>` entero y no
 * solo su `<summary>` porque lo que la regla permite esconder es el bloque
 * plegado; comprobar contra el HTML completo daría por incumplida una vista
 * que cumple, y una guarda que no se puede satisfacer acaba desactivada.
 *
 * Y se quita el SVG antes de buscar: dentro van coordenadas y `aria-label`,
 * que no son prosa de la vista principal. El rótulo accesible de un gráfico
 * SÍ tiene que decir lo que el gráfico dice -eso lo vigila `barraR`-, pero no
 * es el párrafo que la regla 2 persigue.
 */
const PROHIBIDO_EN_LA_PRINCIPAL = [
  ["p corregida", /p\s+corregida/gi],
  ["percentil", /percentil/gi],
  ["mitad central", /mitad\s+central/gi],
  ["correlación", /correlaci[oó]n/gi],
  ["de rangos / Spearman", /spearman|de\s+rangos/gi],
  ["significativ*", /significativ/gi],
  ["p = …", /\bp\s*=\s*[\d−-]/gi],
  ["r = …", /\br\s*=\s*[\d−-]/gi],
  ["n = …", /\bn\s*=\s*\d/gi],
  ["IC 95 / intervalo de confianza", /IC\s*95|intervalo\s+de\s+confianza/gi],
];

/* LOS `<details>` SE ANIDAN, y con un `.replace` suelto eso se cuenta mal.
 *
 * `<details[\s\S]*?<\/details>` es perezoso, así que en un plegable que lleva
 * plegables dentro -"Todas las sesiones de la ventana", con un "ver detalle"
 * por sesión- empareja el `<details>` de fuera con el PRIMER `</details>` de
 * dentro. Todo lo que viene después, que en el móvil está escondido, se le
 * cuenta a la vista principal.
 *
 * El error cae del lado estricto -acusa de enseñar algo que no se ve-, que para
 * un descuido es el lado bueno, pero sigue siendo un número falso: una vista
 * puede ponerse roja por una palabra que nadie va a leer, y de ahí a aflojar la
 * lista de prohibidas hay un paso.
 *
 * Se recortan de dentro afuera. El bucle quita los `<details>` que no contienen
 * ningún otro; los de fuera se quedan entonces sin anidados y les toca en la
 * vuelta siguiente. Se para cuando una pasada no quita nada, así que un HTML con
 * un `<details>` sin cerrar termina igual en vez de dar vueltas para siempre.
 */
function sinPlegables(html) {
  const soloUno = /<details\b[^>]*>(?:(?!<details\b)[\s\S])*?<\/details>/gi;
  let antes = html;
  for (;;) {
    const despues = antes.replace(soloUno, " ");
    if (despues === antes) return despues;
    antes = despues;
  }
}

function textoDeLaPrincipal(html) {
  return sinPlegables(html)
    .replace(/<svg[\s\S]*?<\/svg>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function reglaDosIncumplida(v, html) {
  const texto = textoDeLaPrincipal(html);
  const enc = PROHIBIDO_EN_LA_PRINCIPAL
    .map(([n, re]) => [n, (texto.match(re) || []).length])
    .filter(([, c]) => c > 0);
  if (!enc.length) return null;
  return (
    `la vista principal enseña ${enc.map(([n, c]) => `${n}×${c}`).join(", ")}. ` +
    `La regla 2 manda eso detrás de «ver detalle», no en la pantalla que se ` +
    `abre. Se envuelve con \`plegable()\`, que ya existe en comun.js.`
  );
}

/* LA MITAD QUE IMPIDE EL ATAJO.
 *
 * La regla era ESCONDER lo estadístico, no tirarlo: «Si lo quieres conservar,
 * que esté escondido detrás de un "ver detalle"». Borrarlo pasaría la guarda
 * de arriba con sobresaliente y perdería el dato -y el dato es lo que se abre
 * cuando la frase de encima no basta, que es justo por lo que se calcula-.
 *
 * Así que cada vista que HOY calcula una de esas cosas tiene que seguir
 * ofreciéndola, plegada. La lista es la medición del 2026-09-17 sobre el panel
 * vivo, no una aspiración: desfase y auditoría no salen porque no traían ni un
 * término, y meterlas aquí sería inventarles una obligación que nunca tuvieron.
 */
const DETALLE_OBLIGADO = {
  portada: [["correlación", /correlaci[oó]n/i]],
  concordancia: [["p corregida", /p\s+corregida/i], ["correlación", /correlaci[oó]n/i]],
  impacto: [["p corregida", /p\s+corregida/i], ["correlación", /correlaci[oó]n/i]],
  percepcion: [["percentil", /percentil/i]],
  umbral: [["p corregida", /p\s+corregida/i], ["mitad central", /mitad\s+central/i]],
};

function detalleTirado(v, html) {
  const texto = html
    .replace(/<svg[\s\S]*?<\/svg>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ");
  const faltan = (DETALLE_OBLIGADO[v] || [])
    .filter(([, re]) => !re.test(texto))
    .map(([n]) => n);
  if (!faltan.length) return null;
  return (
    `ya no queda ni rastro de ${faltan.join(", ")} en toda la vista. La regla ` +
    `2 era esconder eso detrás de «ver detalle», no borrarlo: el detalle es lo ` +
    `que se abre cuando la frase de arriba no basta.`
  );
}

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
  const faltan = (EXIGIDOS[v] || []).filter((s) => !html.includes(s));
  if (faltan.length) {
    console.log(
      `FALLO ${v}: pintó ${html.length} bytes y en ninguno aparece ` +
      faltan.join(", "),
    );
    fallos++;
    continue;
  }
  const malPintada = tablaMalPintada(v, html);
  if (malPintada) {
    console.log(`FALLO ${v}: ${malPintada}`);
    fallos++;
    continue;
  }
  // El ranking se pinta DENTRO de la vista de impacto -va detrás del mismo
  // desplegable-, así que se mira aquí y no en una vista propia.
  const sinCorreccion = v === "impacto" ? correccionNoPintada(html) : null;
  if (sinCorreccion) {
    console.log(`FALLO ${v}: ${sinCorreccion}`);
    fallos++;
    continue;
  }
  const contraRegla2 = reglaDosIncumplida(v, html);
  if (contraRegla2) {
    console.log(`FALLO ${v}: ${contraRegla2}`);
    fallos++;
    continue;
  }
  const tirado = detalleTirado(v, html);
  if (tirado) {
    console.log(`FALLO ${v}: ${tirado}`);
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

/* LA SECCIÓN QUE EXPLICA LOS VACÍOS, CON LA LISTA VACÍA.
 *
 * No se llega a este caso pintando la portada: el payload sembrado nunca trae
 * los tres contadores por encima del umbral, así que el bucle de arriba solo
 * ejercita la rama con fichas. Y la rama que faltaba por mirar es justo la que
 * estaba mal: `bloqueLoQueFalta([])` devolvía la cadena vacía, o sea que el
 * bloque cuyo trabajo es distinguir «aquí no pasa nada» de «aquí falta un dato»
 * desaparecía de la pantalla sin decir cuál de las dos cosas era. Un vacío que
 * se explica a sí mismo mientras hay huecos y se calla cuando no los hay es la
 * peor de las dos opciones, porque el día bueno se lee igual que el día roto.
 *
 * Se llama a la función directamente y no por la vista porque lo que se vigila
 * es la rama, no el montaje; el montaje ya lo cubre el bucle. */
const sinHuecos = vm.runInContext("bloqueLoQueFalta([])", contexto);
if (!sinHuecos || !sinHuecos.includes("<h2")) {
  console.log(
    `FALLO portada: \`bloqueLoQueFalta([])\` no pinta nada ` +
    `(${JSON.stringify(sinHuecos)}). Con las cuatro preguntas ya contestables, ` +
    `la sección que explica los vacíos se borra en silencio.`,
  );
  fallos++;
}

if (inventadas.size) {
  console.log(`\nCLAVES QUE EL PAYLOAD NO TRAE (${inventadas.size}):`);
  for (const k of [...inventadas].sort()) console.log(`  ${k}`);
  fallos++;
}

process.exit(fallos ? 1 : 0);
