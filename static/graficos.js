/*
 * Los gráficos, en SVG escrito a mano.
 *
 * POR QUÉ NO HAY LIBRERÍA
 * -----------------------
 * Esto se sirve desde un contenedor en la red local, sin salida a internet y con
 * un service worker que tiene que poder cachearlo todo para abrir sin conexión.
 * Una librería de gráficos por CDN convierte "abro las métricas en el móvil" en
 * "abro las métricas si hay internet", y meterla en el repositorio son cientos de
 * kilobytes de código que nadie de aquí ha leído para dibujar seis formas.
 *
 * DÓNDE ESTÁ EXACTAMENTE LA FRONTERA DEL CÁLCULO
 * ----------------------------------------------
 * Aquí se divide y se multiplica, y eso no contradice la regla de que la PWA no
 * calcula: lo que se calcula son PÍXELES. Un valor entre -1 y 1 tiene que
 * convertirse en una anchura en algún sitio, y ese sitio es este.
 *
 * La frontera es esta: ningún NÚMERO QUE SE LEE sale de una cuenta hecha aquí.
 * Las etiquetas, los porcentajes y los n se pintan tal como llegaron del
 * servidor. Si alguna vez hace falta un número que el payload no trae, la
 * respuesta es añadirlo al backend -donde hay tests con resultado conocido- y no
 * calcularlo en el móvil.
 *
 * TODO GRÁFICO LLEVA SU LECTURA AL LADO
 * -------------------------------------
 * Ninguna de estas funciones se pinta sola. Un dibujo sin su `n`, su ventana y su
 * motivo cuando no hay datos es precisamente lo que esta sección no quiere ser:
 * algo que parece decir más de lo que sabe.
 */

const SVG = "http://www.w3.org/2000/svg";

/* El SVG se escribe como texto y se mete con `innerHTML`, así que todo lo que
 * venga de fuera tiene que ir escapado. `escapar` vive en `comun.js`, que se
 * carga antes; esta comprobación está para que el fallo se vea al arrancar y no
 * en forma de etiquetas raras a mitad de pantalla. */
if (typeof escapar !== "function") {
  throw new Error("graficos.js necesita comun.js cargado antes");
}

// ---------------------------------------------------------------------------
// Colores
// ---------------------------------------------------------------------------

/* Un color por SIGNO, no por "bueno o malo".
 *
 * La tentación es pintar de rojo lo que sube la molestia y de verde lo que la
 * baja. No se hace: el mismo color tendría que significar cosas contrarias según
 * la fila -una correlación positiva con la HRV es buena y con la lumbar es mala-
 * y una pantalla donde el verde a veces significa mal es peor que una sin color.
 * El signo lo dice la dirección de la barra, que no se puede malinterpretar.
 */
const AZUL = "#4c8dff";
const NARANJA = "#e08a3c";
const TENUE = "#5b6472";
const REJILLA = "#2b313b";

function colorSigno(r) {
  if (r === null || r === undefined) return TENUE;
  return r >= 0 ? AZUL : NARANJA;
}

// ---------------------------------------------------------------------------
// La barra de correlación
// ---------------------------------------------------------------------------

/* Una correlación de -1 a 1, con el cero en el centro y marcado.
 *
 * El cero va dibujado y no implícito: sin la línea, una barra corta a la derecha
 * y una corta a la izquierda se parecen demasiado, y son lo contrario.
 *
 * Una correlación que el servidor marcó como no significativa se pinta hueca
 * -solo el contorno-. No se esconde: está ahí, con su n al lado, porque
 * esconderla dejaría la pantalla enseñando solo lo que salió. Pero tampoco se
 * pinta igual de sólida que una que aguanta la corrección por comparaciones
 * múltiples, porque entonces la pantalla diría que las dos cosas son lo mismo.
 *
 * `significativa` tiene TRES valores y los tres significan cosas distintas:
 *
 *   `false` -> se corrigió y no aguanta la corrección. Hueca.
 *   `true`  -> se corrigió y aguanta. Sólida, y con la frase escrita al lado.
 *   `null`  -> sobre esta vista no se pasó ninguna corrección. No hay nada que
 *              aguantar, así que lo único que puede decidir es la n.
 *
 * El tercero son las siete parejas de percepción de la vista 1 y la rejilla de
 * la vista 2: hipótesis declaradas de antemano, cada una con el signo que se
 * espera escrito debajo, que es una pregunta distinta de rastrear una rejilla a
 * ver qué sale. Por eso la línea "aguanta la corrección" solo aparece donde de
 * verdad hubo corrección: escribirla en las cinco vistas la convertiría en
 * decoración.
 *
 * Y por eso la vista 1 lleva LOS TRES valores a la vez, que es la mejor prueba
 * de que la distinción no es teórica. Sus siete parejas de percepción llegan con
 * `null` -nadie las corrigió, eran siete preguntas- y sus diez correlaciones
 * internas del reloj llegan con `true` o `false`, porque esas sí son la rejilla
 * completa de lo que se puede cruzar. Dos bloques en la misma pantalla, con la
 * misma barra, significando cosas distintas y diciéndolo.
 *
 * Aquí ponía lo mismo pero leyendo una clave que concordancia no mandaba.
 * Funcionaba -`undefined !== false` da `true`- y por eso es peligroso: nadie
 * decidió ese comportamiento, salió solo. El servidor manda ahora las tres
 * posibilidades de forma explícita.
 */
function barraR(c, ancho = 260, alto = 22) {
  if (c.r === null || c.r === undefined) {
    return (
      `<svg class="g-barra" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
      `height="${alto}" role="img" aria-label="sin correlación calculada">` +
      `<line x1="${ancho / 2}" y1="2" x2="${ancho / 2}" y2="${alto - 2}" ` +
      `stroke="${REJILLA}" stroke-width="2"/>` +
      `<text x="${ancho / 2 + 8}" y="${alto / 2 + 4}" fill="${TENUE}" ` +
      `font-size="11">sin calcular</text></svg>`
    );
  }

  const r = Math.max(-1, Math.min(1, Number(c.r)));
  const medio = ancho / 2;
  const largo = Math.abs(r) * (medio - 6);
  const x = r >= 0 ? medio : medio - largo;
  const color = colorSigno(r);
  const solida = c.significativa !== false && c.suficiente !== false;

  return (
    `<svg class="g-barra" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `height="${alto}" role="img" aria-label="correlación ${escapar(num(c.r))}">` +
    `<rect x="0" y="${alto / 2 - 1}" width="${ancho}" height="2" fill="${REJILLA}"/>` +
    `<rect x="${x}" y="4" width="${Math.max(largo, 1.5)}" height="${alto - 8}" ` +
    (solida
      ? `fill="${color}" rx="2"/>`
      : `fill="none" stroke="${color}" stroke-width="1.5" rx="2"/>`) +
    `<line x1="${medio}" y1="0" x2="${medio}" y2="${alto}" stroke="${TENUE}" ` +
    `stroke-width="1"/></svg>`
  );
}

// ---------------------------------------------------------------------------
// La curva de desfase
// ---------------------------------------------------------------------------

/* La misma pareja medida de -3 a +3 días, y dónde está el pico.
 *
 * Es el gráfico de la vista 2 y el único que contesta su pregunta de un vistazo:
 * si la percepción se adelanta al reloj, la curva no tiene el máximo en el cero.
 *
 * Los desfases que el servidor no pudo calcular se dibujan como un hueco con una
 * marca tenue abajo, no como un cero. Un punto en la línea del cero diría "ahí
 * no hay relación" cuando lo que pasa es que ahí no hay días suficientes, y esa
 * confusión es justo la que rompería la lectura de la curva.
 */
function curvaDesfase(porDesfase, mejor, ancho = 300, alto = 110) {
  const claves = Object.keys(porDesfase)
    .map(Number)
    .sort((a, b) => a - b);
  if (!claves.length) return "";

  const izq = 26, der = 8, arr = 8, aba = 20;
  const w = ancho - izq - der;
  const h = alto - arr - aba;
  const x = (d) => izq + ((d - claves[0]) / (claves[claves.length - 1] - claves[0])) * w;
  const y = (r) => arr + ((1 - r) / 2) * h;   // r = 1 arriba, r = -1 abajo

  const piezas = [];

  // Rejilla: el cero horizontal -no hay relación- y el cero vertical -el mismo
  // día-. Los dos van nombrados: una línea sin etiqueta se lee como decoración.
  piezas.push(
    `<line x1="${izq}" y1="${y(0)}" x2="${ancho - der}" y2="${y(0)}" ` +
    `stroke="${REJILLA}" stroke-width="1"/>`,
    `<line x1="${x(0)}" y1="${arr}" x2="${x(0)}" y2="${arr + h}" ` +
    `stroke="${REJILLA}" stroke-width="1" stroke-dasharray="3 3"/>`,
    `<text x="2" y="${y(1) + 4}" fill="${TENUE}" font-size="10">+1</text>`,
    `<text x="2" y="${y(0) + 4}" fill="${TENUE}" font-size="10">0</text>`,
    `<text x="2" y="${y(-1) + 4}" fill="${TENUE}" font-size="10">−1</text>`,
  );

  const conDato = claves.filter((d) => {
    const v = porDesfase[d];
    return v && v.r !== null && v.r !== undefined;
  });

  // La línea solo une puntos consecutivos que existen los dos. Un tramo recto
  // sobre un desfase sin calcular inventaría el camino entre dos medidas.
  let camino = "";
  for (let i = 0; i < conDato.length; i++) {
    const d = conDato[i];
    const anterior = conDato[i - 1];
    const seguido = anterior !== undefined && d - anterior === 1;
    camino += `${seguido ? "L" : "M"}${x(d).toFixed(1)},${y(porDesfase[d].r).toFixed(1)}`;
  }
  if (camino) {
    piezas.push(
      `<path d="${camino}" fill="none" stroke="${AZUL}" stroke-width="2" ` +
      `stroke-linejoin="round"/>`,
    );
  }

  for (const d of claves) {
    const v = porDesfase[d] || {};
    const etiqueta = d > 0 ? `+${d}` : String(d);
    piezas.push(
      `<text x="${x(d)}" y="${alto - 6}" fill="${TENUE}" font-size="10" ` +
      `text-anchor="middle">${etiqueta}</text>`,
    );
    if (v.r === null || v.r === undefined) {
      // El hueco, dicho: una crucecita abajo, fuera del área de la curva.
      piezas.push(
        `<text x="${x(d)}" y="${arr + h + 2}" fill="${TENUE}" font-size="9" ` +
        `text-anchor="middle">·</text>`,
      );
      continue;
    }
    const esMejor = mejor !== null && mejor !== undefined && Number(mejor) === d;
    piezas.push(
      `<circle cx="${x(d).toFixed(1)}" cy="${y(v.r).toFixed(1)}" ` +
      `r="${esMejor ? 5 : 3}" fill="${esMejor ? NARANJA : AZUL}"/>`,
    );
  }

  return (
    `<svg class="g-curva" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `role="img" aria-label="correlación por desfase de días">` +
    piezas.join("") + `</svg>`
  );
}

// ---------------------------------------------------------------------------
// El calendario de semáforos
// ---------------------------------------------------------------------------

const COLOR_LUZ = { green: "#2f9e5e", amber: "#d99b21", red: "#d1453b" };

/* Los días de la ventana en cuadrículas, una columna por semana.
 *
 * Un día SIN decisión no es un día verde ni un hueco invisible: es un cuadro con
 * el borde marcado y el interior vacío. Que se vea. En una vista que existe para
 * auditar al motor, los días en que el motor no decidió son parte de lo que hay
 * que auditar -y son los que más fácilmente desaparecerían de un calendario
 * dibujado sin pensar-.
 *
 * Empieza en lunes porque la distribución semanal del payload va por semanas ISO,
 * y dos rejillas de la misma pantalla cortando la semana por sitios distintos se
 * leen como dos periodos distintos.
 */
function calendario(dias, lado = 13, hueco = 3) {
  if (!dias || !dias.length) return "";

  // `getUTCDay` sobre una fecha construida con `Date.UTC`: se quiere el día de
  // la semana del día de calendario, sin que la zona horaria del móvil lo mueva.
  const diaSemana = (iso) => {
    const [a, m, d] = iso.split("-").map(Number);
    return (new Date(Date.UTC(a, m - 1, d)).getUTCDay() + 6) % 7;  // 0 = lunes
  };

  const paso = lado + hueco;
  let columna = 0;
  let previo = null;
  const celdas = [];

  for (const d of dias) {
    const fila = diaSemana(d.fecha);
    if (previo !== null && fila <= previo) columna++;
    previo = fila;

    const luz = d.luz && COLOR_LUZ[d.luz];
    const titulo = d.luz
      ? `${d.fecha}: ${d.nombre_luz || d.luz}` +
        (d.regla_determinante ? ` (${d.regla_determinante})` : "")
      : `${d.fecha}: ${d.na || "sin decisión guardada"}`;

    celdas.push(
      `<rect x="${columna * paso}" y="${fila * paso}" width="${lado}" ` +
      `height="${lado}" rx="2.5" ` +
      (luz
        ? `fill="${luz}">`
        : `fill="none" stroke="${REJILLA}" stroke-width="1.5">`) +
      `<title>${escapar(titulo)}</title></rect>`,
    );
  }

  const ancho = (columna + 1) * paso;
  return (
    `<svg class="g-calendario" viewBox="0 0 ${ancho} ${7 * paso}" ` +
    `width="${ancho}" height="${7 * paso}" role="img" ` +
    `aria-label="calendario de semáforos por día">` + celdas.join("") + `</svg>`
  );
}

// ---------------------------------------------------------------------------
// La distribución semanal
// ---------------------------------------------------------------------------

/* Una barra por semana, apilada verde/ámbar/rojo, más lo que no se decidió.
 *
 * Los siete días de cada semana siempre suman la barra entera, así que la altura
 * de cada trozo sale de dividir entre el total de días de la semana y no entre
 * los días con decisión. Dividido entre los decididos, una semana con un solo
 * día verde saldría igual de verde que una semana entera de verdes, y la vista
 * se pidió justo para ver cuánto decidió el motor y con qué.
 */
function barrasSemanales(semanas, nombres, alto = 64) {
  if (!semanas || !semanas.length) return "";

  const anchoBarra = 10, hueco = 3;
  const piezas = [];
  // Igual que en el resto de la vista: los nombres de los colores vienen del
  // servidor. Estaban escritos a mano justo aquí dentro, y una tabla escondida
  // en el título de un `<rect>` es la que nadie se acuerda de tocar.
  const nombre = (luz) => (nombres || {})[luz] || luz;

  semanas.forEach((s, i) => {
    const x = i * (anchoBarra + hueco);
    const total = (s.n || 0) + (s.sin_decision || 0);
    if (!total) return;

    let y = 0;
    const trozo = (cuenta, relleno, borde) => {
      if (!cuenta) return;
      const h = (cuenta / total) * alto;
      piezas.push(
        `<rect x="${x}" y="${y.toFixed(1)}" width="${anchoBarra}" ` +
        `height="${h.toFixed(1)}" ` +
        (borde
          ? `fill="none" stroke="${REJILLA}" stroke-width="1"/>`
          : `fill="${relleno}"/>`),
      );
      y += h;
    };

    trozo(s.red, COLOR_LUZ.red);
    trozo(s.amber, COLOR_LUZ.amber);
    trozo(s.green, COLOR_LUZ.green);
    trozo(s.sin_decision, null, true);

    piezas.push(
      `<rect x="${x}" y="0" width="${anchoBarra}" height="${alto}" fill="transparent">` +
      `<title>${escapar(
        `Semana ${s.semana} (${s.desde} → ${s.hasta}): ` +
        `${s.green} ${nombre("green")}, ${s.amber} ${nombre("amber")}, ` +
        `${s.red} ${nombre("red")}, ${s.sin_decision} sin decisión`,
      )}</title></rect>`,
    );
  });

  const ancho = semanas.length * (anchoBarra + hueco);
  return (
    `<svg class="g-semanas" viewBox="0 0 ${ancho} ${alto}" width="${ancho}" ` +
    `height="${alto}" role="img" aria-label="reparto de luces por semana">` +
    piezas.join("") + `</svg>`
  );
}

// ---------------------------------------------------------------------------
// Percepción frente a rendimiento
// ---------------------------------------------------------------------------

/* Dos marcas sobre la misma regla de 0 a 100, y la distancia entre ellas.
 *
 * Es el dibujo de la vista 5 y el que tiene que poder leerse de un vistazo una
 * mañana mala: la marca de abajo es lo que se esperaba de uno mismo, la de arriba
 * lo que salió, y cuando la de arriba está muy a la derecha de la otra, eso es la
 * disociación. Sin flecha entre las dos, porque la flecha sugiere causa.
 *
 * Las dos marcas son percentiles CONTRA SU PROPIO HISTÓRICO, no notas absolutas.
 * La regla va rotulada con eso para que un 40 no se lea como un suspenso: un 40
 * quiere decir que ese día fue mejor que el 40 % de sus días, que en una racha
 * mala puede ser una sesión excelente.
 */
function reglaPercentiles(percepcionPct, rendimientoPct, ancho = 300, alto = 62) {
  const izq = 6, der = 6;
  const w = ancho - izq - der;
  const x = (p) => izq + (Math.max(0, Math.min(100, Number(p))) / 100) * w;
  const yReg = 34;
  const piezas = [];

  piezas.push(
    `<rect x="${izq}" y="${yReg - 2}" width="${w}" height="4" rx="2" fill="${REJILLA}"/>`,
    `<text x="${izq}" y="${alto - 4}" fill="${TENUE}" font-size="10">peor día</text>`,
    `<text x="${ancho - der}" y="${alto - 4}" fill="${TENUE}" font-size="10" ` +
    `text-anchor="end">mejor día</text>`,
  );

  const hay = (p) => p !== null && p !== undefined;

  if (hay(percepcionPct) && hay(rendimientoPct)) {
    const a = x(percepcionPct), b = x(rendimientoPct);
    piezas.push(
      `<rect x="${Math.min(a, b)}" y="${yReg - 2}" ` +
      `width="${Math.abs(b - a)}" height="4" fill="${NARANJA}" opacity="0.55"/>`,
    );
  }

  if (hay(percepcionPct)) {
    piezas.push(
      `<circle cx="${x(percepcionPct)}" cy="${yReg}" r="6" fill="none" ` +
      `stroke="${TENUE}" stroke-width="2"/>` +
      `<text x="${x(percepcionPct)}" y="${yReg + 22}" fill="${TENUE}" ` +
      `font-size="10" text-anchor="middle">esperabas</text>`,
    );
  }
  if (hay(rendimientoPct)) {
    piezas.push(
      `<circle cx="${x(rendimientoPct)}" cy="${yReg}" r="6" fill="${AZUL}"/>` +
      `<text x="${x(rendimientoPct)}" y="${yReg - 12}" fill="${AZUL}" ` +
      `font-size="10" text-anchor="middle">hiciste</text>`,
    );
  }

  return (
    `<svg class="g-regla" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `role="img" aria-label="percentil esperado frente a percentil hecho">` +
    piezas.join("") + `</svg>`
  );
}

// ---------------------------------------------------------------------------
// La HRV en el tiempo, con las salidas debajo
// ---------------------------------------------------------------------------

/* Media año de HRV y, en su propio carril, cada salida como un palo.
 *
 * Es el dibujo que contesta "¿se ve?" antes de que ninguna media diga nada. Las
 * dos señales van en carriles SEPARADOS y no superpuestas a propósito: pintar la
 * carga sobre la HRV obliga a dos ejes verticales distintos en la misma caja, y
 * dos ejes en una caja de 160 píxeles en un móvil es donde nacen las lecturas
 * inventadas -"mira cómo baja justo cuando sube el palo"- que el ojo ve tanto si
 * están como si no.
 *
 * SIN EJE NUMÉRICO, y no por falta de sitio. Un eje de milisegundos invita a leer
 * valores absolutos de una HRV, que no significan nada fuera de esta persona. Lo
 * que sí va dibujado es la BANDA de lo normal -sus propios cuartiles-, que es la
 * referencia que sí se puede leer: dentro de la banda es un día como otro
 * cualquiera, fuera no. Los números de la banda van en la prosa de al lado, donde
 * se leen una vez, y no repetidos en el borde de la caja.
 *
 * La línea gruesa es la media móvil que manda el servidor. Los puntos sueltos de
 * detrás son la medida cruda de cada noche, tenues: sin ellos, una línea suave
 * sobre una señal que salta diez milisegundos cada noche parece un dato mucho más
 * firme de lo que es.
 *
 * Los días sin medir no se unen, igual que en `serieTemporal`, y por lo mismo.
 */
function hrvConSalidas(g, ancho = 320, alto = 160) {
  if (!g || !g.puntos || !g.puntos.length) return "";
  const rango = g.rango;
  if (!rango || rango.length !== 2 || rango[0] === rango[1]) return "";

  // Tres carriles: la HRV arriba, un respiro, y las salidas abajo. Las salidas
  // crecen HACIA ARRIBA desde su base, que es como se lee "cuánto fue" sin
  // pensar.
  const arrH = 6, altoH = 92;          // carril de la HRV
  const baseS = alto - 16, altoS = 40; // carril de las salidas
  const izq = 2, der = 2;
  const w = ancho - izq - der;

  const aNum = (iso) => {
    const [a, m, d] = String(iso).split("-").map(Number);
    return Date.UTC(a, m - 1, d) / 86400000;
  };
  const t0 = aNum(g.puntos[0].fecha);
  const t1 = aNum(g.puntos[g.puntos.length - 1].fecha);
  const span = t1 === t0 ? 1 : t1 - t0;
  const x = (iso) => izq + ((aNum(iso) - t0) / span) * w;
  const y = (v) => arrH + (1 - (v - rango[0]) / (rango[1] - rango[0])) * altoH;

  const piezas = [];

  // La banda de lo normal, detrás de todo.
  if (g.banda && g.banda.desde !== null && g.banda.hasta !== null) {
    const yA = y(g.banda.hasta), yB = y(g.banda.desde);
    piezas.push(
      `<rect x="${izq}" y="${yA.toFixed(1)}" width="${w}" ` +
      `height="${Math.max(yB - yA, 1).toFixed(1)}" fill="${AZUL}" opacity="0.10">` +
      `<title>${escapar(
        `lo normal en ti: de ${g.banda.desde} a ${g.banda.hasta} ms`,
      )}</title></rect>`,
    );
  }

  // La medida cruda de cada noche, tenue y sin unir.
  for (const p of g.puntos) {
    if (p.valor === null || p.valor === undefined) continue;
    piezas.push(
      `<circle cx="${x(p.fecha).toFixed(1)}" cy="${y(p.valor).toFixed(1)}" ` +
      `r="0.9" fill="${TENUE}" opacity="0.55"/>`,
    );
  }

  // Y encima la media móvil, que es la que se sigue con el ojo.
  let camino = "";
  let previo = null;
  for (const p of g.puntos) {
    if (p.suave === null || p.suave === undefined) { previo = null; continue; }
    const n = aNum(p.fecha);
    const seguido = previo !== null && n - previo === 1;
    camino += `${seguido ? "L" : "M"}${x(p.fecha).toFixed(1)},${y(p.suave).toFixed(1)}`;
    previo = n;
  }
  if (camino) {
    piezas.push(
      `<path d="${camino}" fill="none" stroke="${AZUL}" stroke-width="2" ` +
      `stroke-linejoin="round" stroke-linecap="round"/>`,
    );
  }

  // El suelo del carril de abajo, que es lo que convierte los palos en palos y
  // no en marcas flotando.
  piezas.push(
    `<line x1="${izq}" y1="${baseS}" x2="${ancho - der}" y2="${baseS}" ` +
    `stroke="${REJILLA}" stroke-width="1"/>`,
  );

  // La raya del umbral cruzando el carril, con su altura ya calculada en el
  // servidor. Es la que hace que el dibujo conteste la pregunta de la vista:
  // los palos que la pasan son los que cuestan un día.
  if (g.altura_corte !== null && g.altura_corte !== undefined) {
    const yC = baseS - Number(g.altura_corte) * altoS;
    piezas.push(
      `<line x1="${izq}" y1="${yC.toFixed(1)}" x2="${ancho - der}" ` +
      `y2="${yC.toFixed(1)}" stroke="${NARANJA}" stroke-width="1" ` +
      `stroke-dasharray="4 3" opacity="0.8"><title>${escapar(
        `el escalón: ${g.corte} de carga`,
      )}</title></line>`,
    );
  }

  for (const s of g.salidas || []) {
    const h = Math.max(Number(s.altura) * altoS, 1);
    // `dura` viene en tres estados y los tres se pintan distinto: `true` es una
    // salida por encima del escalón, `false` por debajo, y `null` es que no se
    // encontró escalón -entonces no hay dos clases de salida y se pintan todas
    // igual, en vez de fingir una distinción que esta ventana no sostiene-.
    const color = s.dura === true ? NARANJA : s.dura === false ? TENUE : AZUL;
    piezas.push(
      `<rect x="${(x(s.fecha) - 0.9).toFixed(1)}" ` +
      `y="${(baseS - h).toFixed(1)}" width="1.8" height="${h.toFixed(1)}" ` +
      `fill="${color}" opacity="${s.dura === false ? "0.7" : "0.95"}">` +
      `<title>${escapar(`${s.fecha}: ${s.carga} de carga`)}</title></rect>`,
    );
  }

  // Las dos fechas de los extremos. No son un eje: son el ancla sin la cual
  // media año de línea no se sabe dónde empieza.
  piezas.push(
    `<text x="${izq}" y="${alto - 3}" fill="${TENUE}" font-size="9">` +
    `${escapar(fechaMinima(g.puntos[0].fecha))}</text>`,
    `<text x="${ancho - der}" y="${alto - 3}" fill="${TENUE}" font-size="9" ` +
    `text-anchor="end">${escapar(
      fechaMinima(g.puntos[g.puntos.length - 1].fecha),
    )}</text>`,
  );

  return (
    `<svg class="g-hrv" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `role="img" aria-label="la HRV a lo largo de la ventana con las salidas ` +
    `de bici marcadas debajo">` + piezas.join("") + `</svg>`
  );
}

// ---------------------------------------------------------------------------
// La curva de recuperación
// ---------------------------------------------------------------------------

/* Cuánto se movió la HRV el día +1, +2, +3 y +4 de una salida.
 *
 * El cero va dibujado, etiquetado y ES LA REFERENCIA: aquí no se mide una HRV,
 * se mide un cambio respecto de la noche anterior a la salida. Una barra hacia
 * abajo es "peor que antes de salir" y una hacia arriba es "mejor". Sin la línea
 * del cero rotulada, la altura de una barra no significa nada.
 *
 * Cada barra lleva su BIGOTE entre cuartiles, y ésa es la pieza que impide la
 * lectura fácil. Cuatro medias solas dibujan una curva de recuperación preciosa;
 * con los bigotes se ve que el día +1 baja seis milisegundos de media con
 * salidas que fueron de −7,5 a −5, y que el día +3 tiene un recorrido tan ancho
 * que su media es casi una anécdota. Las dos cosas son el mismo dato y solo una
 * de las dos se puede presumir.
 *
 * Y NO HAY BARRA HUECA aquí, que en el resto del panel significa "no aguanta la
 * corrección". Estas medias no llevan contraste ninguno -el servidor manda
 * `significativa: null` a propósito- y pintarlas huecas diría que suspendieron un
 * examen al que no se presentaron.
 */
function barrasRecuperacion(curva, ancho = 300, alto = 130) {
  const dias = (curva && curva.por_dia) || [];
  if (!dias.length) return "";
  const escala = Number(curva.escala);
  if (!escala || !isFinite(escala)) return "";

  const arr = 10, aba = 26;
  const h = alto - arr - aba;
  const yCero = arr + h / 2;
  const izq = 22, der = 6;
  const w = ancho - izq - der;
  const paso = w / dias.length;
  const anchoBarra = Math.min(paso * 0.5, 26);
  // Media caja para la escala entera: una barra de `escala` llega justo al
  // borde, y ninguna se sale.
  const y = (v) => yCero - (Number(v) / escala) * (h / 2);

  const piezas = [
    `<line x1="${izq - 6}" y1="${yCero}" x2="${ancho - der}" y2="${yCero}" ` +
    `stroke="${TENUE}" stroke-width="1"/>`,
    `<text x="0" y="${yCero - 4}" fill="${TENUE}" font-size="9">igual</text>`,
    `<text x="0" y="${yCero + 11}" fill="${TENUE}" font-size="9">que</text>`,
    `<text x="0" y="${yCero + 21}" fill="${TENUE}" font-size="9">antes</text>`,
  ];

  dias.forEach((d, i) => {
    const cx = izq + paso * i + paso / 2;
    piezas.push(
      `<text x="${cx.toFixed(1)}" y="${alto - 12}" fill="${TENUE}" ` +
      `font-size="10" text-anchor="middle">+${escapar(d.dia)}</text>`,
    );

    if (d.media === null || d.media === undefined) {
      // El día sin media se dice con palabras, no con una barra de altura cero
      // -que se leería como "ese día no se movió"-.
      piezas.push(
        `<text x="${cx.toFixed(1)}" y="${(yCero + 4).toFixed(1)}" ` +
        `fill="${TENUE}" font-size="9" text-anchor="middle">·` +
        `<title>${escapar(d.na || "sin media")}</title></text>`,
      );
      return;
    }

    const yV = y(d.media);
    const color = colorSigno(d.media);
    piezas.push(
      `<rect x="${(cx - anchoBarra / 2).toFixed(1)}" ` +
      `y="${Math.min(yV, yCero).toFixed(1)}" width="${anchoBarra.toFixed(1)}" ` +
      `height="${Math.max(Math.abs(yV - yCero), 1).toFixed(1)}" ` +
      `fill="${color}" rx="1.5"><title>${escapar(
        `día +${d.dia}: ${d.frase}` +
        (d.p25 !== null && d.p25 !== undefined
          ? ` · la mitad central entre ${d.p25} y ${d.p75}`
          : "") +
        ` · ${d.n}`,
      )}</title></rect>`,
    );

    if (d.p25 === null || d.p25 === undefined) return;
    const yA = y(d.p75), yB = y(d.p25);
    const ala = Math.min(anchoBarra * 0.3, 6);
    piezas.push(
      `<line x1="${cx.toFixed(1)}" y1="${yA.toFixed(1)}" x2="${cx.toFixed(1)}" ` +
      `y2="${yB.toFixed(1)}" stroke="${TENUE}" stroke-width="1.2"/>` +
      `<line x1="${(cx - ala).toFixed(1)}" y1="${yA.toFixed(1)}" ` +
      `x2="${(cx + ala).toFixed(1)}" y2="${yA.toFixed(1)}" stroke="${TENUE}" ` +
      `stroke-width="1.2"/>` +
      `<line x1="${(cx - ala).toFixed(1)}" y1="${yB.toFixed(1)}" ` +
      `x2="${(cx + ala).toFixed(1)}" y2="${yB.toFixed(1)}" stroke="${TENUE}" ` +
      `stroke-width="1.2"/>`,
    );
  });

  piezas.push(
    `<text x="${ancho - der}" y="${alto - 1}" fill="${TENUE}" font-size="9" ` +
    `text-anchor="end">días después de la salida</text>`,
  );

  return (
    `<svg class="g-recuperacion" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `role="img" aria-label="cuánto cambia la HRV cada día después de una ` +
    `salida, con el recorrido de la mitad central">` +
    piezas.join("") + `</svg>`
  );
}

// ---------------------------------------------------------------------------
// Serie temporal
// ---------------------------------------------------------------------------

/* Los puntos de una señal a lo largo de la ventana, en su propio rango.
 *
 * El eje vertical va del mínimo al máximo DECLARADOS de la señal (`rango`), no
 * del mínimo al máximo observados. Autoescalando, un cansancio que osciló entre
 * 3 y 4 llenaría la caja entera y parecería que hubo de todo; con la escala
 * declarada se ve lo que fue: una línea plana en el centro.
 *
 * Los días sin medida no se unen. Una raya recta entre el lunes y el viernes
 * dibuja tres días que nadie midió.
 */
function serieTemporal(puntos, rango, campo = "valor", ancho = 300, alto = 46) {
  if (!puntos || !puntos.length) return "";
  const [bajo, alPunto] = rango && rango.length === 2 ? rango : [0, 10];
  if (alPunto === bajo) return "";

  const fechas = puntos.map((p) => p.fecha);
  const primera = fechas[0], ultima = fechas[fechas.length - 1];
  const aNum = (iso) => {
    const [a, m, d] = String(iso).split("-").map(Number);
    return Date.UTC(a, m - 1, d) / 86400000;
  };
  const t0 = aNum(primera), t1 = aNum(ultima);
  const w = t1 === t0 ? 1 : t1 - t0;

  const x = (iso) => ((aNum(iso) - t0) / w) * (ancho - 4) + 2;
  const y = (v) => alto - 3 - ((v - bajo) / (alPunto - bajo)) * (alto - 6);

  let camino = "";
  let previo = null;
  for (const p of puntos) {
    const iso = p.fecha;
    const v = p[campo];
    if (v === null || v === undefined) { previo = null; continue; }
    const seguido = previo !== null && aNum(iso) - previo === 1;
    camino += `${seguido ? "L" : "M"}${x(iso).toFixed(1)},${y(v).toFixed(1)}`;
    previo = aNum(iso);
  }

  return (
    `<svg class="g-serie" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `height="${alto}" role="img" aria-label="la señal a lo largo de la ventana">` +
    `<path d="${camino}" fill="none" stroke="${AZUL}" stroke-width="1.5" ` +
    `stroke-linejoin="round" stroke-linecap="round"/></svg>`
  );
}
