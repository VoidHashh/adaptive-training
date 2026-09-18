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

/* Dos tonos más que SOLO usan los gráficos de un vistazo del final del archivo.
 *
 * `TENUE` sirve para una raya de rejilla y se queda corto para un rótulo que hay
 * que leer: en el móvil, a las seis de la mañana y con brillo bajo, `#5b6472`
 * sobre el fondo oscuro es una mancha. Los ejes del panel nuevo se leen o no
 * sirven de nada -esa es la regla 3-, así que llevan un gris que contrasta.
 * `TEXTO` es el del cuerpo de la página, para lo que va al mismo nivel que la
 * prosa: las etiquetas de debajo de cada barra y la leyenda del quesito. */
const ROTULO = "#9aa4b2";
const TEXTO = "#e7eaef";

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
/* Los dos rótulos de los extremos van en su PROPIO renglón, y por eso la regla
 * mide setenta y seis y no sesenta y dos. «esperabas» cuelga del círculo, que
 * se mueve por toda la barra, y un día en que la percepción cae abajo del todo
 * el círculo se pone encima del cero: las dos palabras compartían línea y salía
 * «peoresperabas». No es un caso raro -la vista existe justo para las mañanas
 * en que uno se veía fatal- y con la regla mirada de lejos parecía una palabra
 * más larga, no dos pisadas. */
function reglaPercentiles(percepcionPct, rendimientoPct, ancho = 300, alto = 76) {
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
      `<text x="${x(percepcionPct)}" y="${yReg + 20}" fill="${TENUE}" ` +
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

/* AQUÍ ESTABA `barrasRecuperacion`, y se ha ido entera.
 *
 * Dibujaba la curva del día +1 al +4 con un bigote entre cuartiles colgando de
 * cada barra, y era buena: lo que enseñaba de verdad era que el día +3 tiene un
 * recorrido tan ancho que su media es casi una anécdota. Pero un bigote entre
 * cuartiles ES la mitad central, y la mitad central está nombrada por su nombre
 * en la regla 2 como algo que no puede salir en la vista principal. La curva la
 * pinta ahora `barrasDeVistazo` con cuatro barras y nada más; los cuartiles
 * siguen enteros dentro de «ver detalle», en la tabla por día.
 *
 * Se borra en vez de dejarse por si acaso porque una función de ciento diez
 * líneas que no llama nadie no es una reserva, es una segunda versión del
 * gráfico esperando a que alguien la vuelva a enchufar sin acordarse de por qué
 * se apagó. */

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

// ===========================================================================
// LOS GRÁFICOS DE UN VISTAZO
// ===========================================================================

/*
 * Barras, línea y quesito. Los tres salen de la maqueta que se aprobó el
 * 2026-09-15 y que vive en `docs/maquetas/panel_umbral.py`; esto es esa maqueta
 * traída al código que se sirve de verdad, no una reinterpretación.
 *
 * QUÉ TIENEN DE DISTINTO LOS DE ARRIBA
 * ------------------------------------
 * `barraR`, `curvaDesfase` y `reglaPercentiles` están bien hechos y contestan
 * preguntas reales, pero contestan preguntas de quien ya sabe qué es una
 * correlación: una barra de -1 a 1 no se lee, se interpreta. Los de aquí abajo
 * tienen un requisito distinto y más duro -«que se entienda sin leer nada»-, y
 * de ahí salen tres diferencias concretas:
 *
 *   - EJES CON NÚMEROS Y UNIDAD. Los de arriba no tienen eje: la escala va
 *     implícita en «de -1 a 1». Aquí la escala se dibuja, con la unidad puesta,
 *     porque nadie sabe de memoria si 7 ms es mucho.
 *   - EL VALOR, ENCIMA DE LA BARRA. Un eje se lee si uno se para a leerlo; el
 *     número sobre la barra entra sin querer. Esa es la diferencia entre un
 *     panel para estudiar y uno para mirar.
 *   - 340 PÍXELES DE ANCHO, TODOS IGUAL. Regla 5. El ancho no está elegido por
 *     bonito: es el que cabe en vertical en el móvil más estrecho que se usa
 *     aquí. Y que midan todos lo mismo importa más que el número, porque el que
 *     se sale un poco es el que obliga a deslizar la pantalla entera.
 *
 * LA FRONTERA DEL CÁLCULO, OTRA VEZ, PORQUE AQUÍ SE ESTRECHA
 * ----------------------------------------------------------
 * La cabecera del archivo dice que ningún número que se lee sale de una cuenta
 * hecha aquí. Estas funciones lo cumplen de la forma incómoda: los valores de
 * las barras se reciben YA ESCRITOS, en `rotulos`, y lo que llega en `valores`
 * solo decide la altura. Es más trabajo para quien llama y se hace igual,
 * porque el servidor redondea sus frases sin pasar por `toFixed` -«baja 0,2 ms»
 * sobre una media de 0,15- y cualquier redondeo hecho aquí acertaría en unos
 * casos y fallaría en otros. Entonces la barra y su detalle dirían números
 * distintos del mismo dato, que es la peor avería posible en una pantalla cuyo
 * argumento es que se entiende sola.
 *
 * La ÚNICA excepción son las marcas del eje, que sí se calculan aquí. Y es una
 * excepción de verdad, no una grieta: una marca de eje no es una medida, es una
 * propiedad de la regla con la que se mide. No existe en el payload porque no
 * hay nada que el servidor pueda decir al respecto sin saber cuántos píxeles de
 * alto tiene el dibujo.
 */

/* Un paso de rejilla que caiga en números que alguien diría en voz alta.
 *
 * Sin esto, cuatro líneas repartidas sobre un recorrido de 7,3 caen en 1,825 y
 * el eje se rotula «0 · 1,8 · 3,7 · 5,5». Son números correctos que nadie usa
 * para medir nada, y obligan a leer el eje en vez de mirarlo. */
function pasoBonito(recorrido, quiero = 5) {
  const bruto = recorrido / Math.max(quiero, 1);
  if (!(bruto > 0)) return 1;
  // La magnitud a base de multiplicar y dividir por diez, y no con `Math.pow`.
  // No es esquivar a `test_la_pwa_no_calcula_estadistica`: es que la guarda
  // tiene razón también aquí. Una potencia en este archivo es una línea que
  // mañana sirve para elevar un dato al cuadrado, y lo único que hace falta en
  // esta función es «el uno seguido de ceros que cabe debajo».
  let mag = 1;
  while (mag * 10 <= bruto) mag *= 10;
  while (mag > bruto) mag /= 10;
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (bruto <= m * mag) return m * mag;
  }
  return 10 * mag;
}

/* El menos de verdad, para lo que se escribe AQUÍ.
 *
 * `num()` de comun.js sale de `toFixed`, que usa el guion del teclado. En una
 * ficha técnica da igual; en una cifra grande encima de una barra, el guion se
 * confunde con un resto de la rejilla y «−7,0» se lee «7,0», o sea el valor con
 * el signo cambiado en el único número que la pantalla quiere que entre solo.
 *
 * No se toca `num()`: sus cifras van dentro de frases y de fichas, y cambiarle
 * el signo a todo el panel desde aquí sería arreglar un gráfico moviendo el
 * suelo de las otras seis vistas.
 *
 * Y UN MENOS DELANTE DE UN CERO SE CAE. «Cualquier salida» baja el cansancio
 * 0,04 puntos, y con un decimal `toFixed` escupe «-0,0»: encima de una barra
 * eso no es un número, es un número roto. El menos promete una dirección y el
 * cero dice que no hay ninguna, así que quien lo mira se para a averiguar cuál
 * de las dos cosas le están contando. Redondear a más decimales no vale -sería
 * poner «−0,04» al lado de un «−0,2» y fingir una precisión que la resta de dos
 * medias no tiene-, y esconder la barra tampoco: la fila tiene su dato y el
 * dato es que ahí no se mueve nada. Así que se queda «0,0», que es exactamente
 * lo que pasa, y el color de la barra ya lleva el sentido. */
function conMenos(s) {
  const t = String(s);
  return /^-0[,.]?0*$/.test(t) ? t.slice(1) : t.replace(/^-/, "−");
}

/* La escala vertical y su rejilla, compartidas por las barras y por la línea.
 *
 * Están juntas a propósito. Cuando cada gráfico se dibujaba su propio eje, los
 * dos de la misma pantalla acababan con distinto número de marcas y con la
 * unidad en sitios distintos, y dos ejes que no se parecen se comparan mal
 * aunque los dos estén bien. */
function escalaY(valores, arr, alto, margen = 0.18) {
  const vs = valores.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  let lo = Math.min(...vs);
  let hi = Math.max(...vs);
  const aire = (hi - lo) * margen || 1;
  lo -= aire;
  hi += aire;
  return { lo, hi, y: (v) => arr + (1 - (v - lo) / (hi - lo)) * alto };
}

/* Los decimales del eje los manda el PASO, no una cifra fija.
 *
 * Estaba clavado a cero decimales, y con eso el eje de «cuánto dura lo de salir
 * corto» -que va de 0 a −0,2 puntos de cansancio- salía rotulado «0 · −0 · −0»:
 * tres marcas distintas con el mismo rótulo, y dos de ellas diciendo menos cero.
 * Un eje así no es que se lea mal, es que miente sobre dónde está cada raya.
 *
 * Con el paso ya calculado la cuenta es la que haría cualquiera a mano: si las
 * rayas van de una en una no hacen falta decimales, si van de una décima hace
 * falta uno. Se corta en dos porque más allá de las centésimas ningún número de
 * este panel significa nada -son restas de medias de unas decenas de días- y un
 * eje con tres decimales solo añade tinta. */
function decimalesDelPaso(paso) {
  if (paso >= 1) return 0;
  if (paso >= 0.1) return 1;
  return 2;
}

function rejillaY(esc, izq, derX, cuantas, conCero) {
  const paso = pasoBonito(esc.hi - esc.lo, cuantas);
  const dec = decimalesDelPaso(paso);
  let t = paso * Math.round(esc.lo / paso);
  let p = "";
  while (t <= esc.hi + 1e-9) {
    if (t >= esc.lo) {
      const yy = esc.y(t).toFixed(1);
      const cero = conCero && Math.abs(t) < 1e-9;
      p +=
        `<line x1="${izq}" y1="${yy}" x2="${derX}" y2="${yy}" ` +
        `stroke="${cero ? ROTULO : REJILLA}" stroke-width="${cero ? 1.4 : 1}"/>` +
        `<text x="${izq - 6}" y="${(esc.y(t) + 4).toFixed(1)}" fill="${ROTULO}" ` +
        `font-size="12" text-anchor="end">` +
        `${escapar(conMenos(num(t, dec)))}</text>`;
    }
    t += paso;
  }
  return p;
}

/* BARRAS VERTICALES ALREDEDOR DEL CERO. El escalón, visto de golpe.
 *
 * Es el gráfico que se pidió por su nombre dos veces: «la caída de HRV por
 * tramo de carga, cuatro barras, se ve el escalón solo» y «la recuperación día
 * a día tras una salida dura, día +1, +2, +3, +4».
 *
 * El cero va DIBUJADO y más grueso que la rejilla. Sin esa raya, una barra
 * corta hacia abajo y una corta hacia arriba se parecen demasiado y son lo
 * contrario; con ella, el lado de la barra ya cuenta la historia antes de leer
 * nada, que es todo el encargo.
 *
 * `valores` da la altura y puede traer `null` -un tramo sin bastantes salidas-.
 * Un `null` deja su etiqueta puesta y no dibuja barra: un cero en su sitio
 * diría «ahí no pasó nada» cuando lo que pasa es que ahí no se ha mirado, y esa
 * es la confusión que el panel entero lleva un año evitando en todas partes.
 */
function barrasDeVistazo({ valores, rotulos, etiquetas, pie, unidad = "", ancho = 340, alto = 236 }) {
  const vs = valores.filter((v) => v !== null && v !== undefined);
  if (!vs.length) return "";

  const izq = 36, der = 10, arr = 24, aba = 70;
  const w = ancho - izq - der, h = alto - arr - aba;
  const esc = escalaY([...vs, 0], arr, h);

  let p = rejillaY(esc, izq, ancho - der, 4, true);

  const pasoX = w / valores.length;
  const anchoBarra = Math.min(pasoX * 0.56, 46);

  valores.forEach((v, i) => {
    const cx = izq + pasoX * i + pasoX / 2;
    for (const [j, linea] of (etiquetas[i] || []).entries()) {
      p +=
        `<text x="${cx.toFixed(1)}" y="${alto - 44 + j * 16}" fill="${TEXTO}" ` +
        `font-size="13" text-anchor="middle">${escapar(linea)}</text>`;
    }
    if (v === null || v === undefined) return;
    const color = v >= 0 ? AZUL : NARANJA;
    const y0 = esc.y(0), y1 = esc.y(v);
    p +=
      `<rect x="${(cx - anchoBarra / 2).toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" ` +
      `width="${anchoBarra.toFixed(1)}" height="${Math.max(Math.abs(y1 - y0), 1.5).toFixed(1)}" ` +
      `fill="${color}" rx="3"/>`;
    // El número, FUERA de la barra. Dentro se pierde en cuanto la barra es
    // corta, y las barras cortas son justo las que hay que poder comparar.
    const ty = v >= 0 ? y1 - 8 : y1 + 17;
    p +=
      `<text x="${cx.toFixed(1)}" y="${ty.toFixed(1)}" fill="${color}" ` +
      `font-size="15" font-weight="700" text-anchor="middle">` +
      `${escapar(conMenos(rotulos[i]))}</text>`;
  });

  if (unidad) {
    p += `<text x="${izq - 34}" y="${arr - 8}" fill="${ROTULO}" font-size="12">${escapar(unidad)}</text>`;
  }
  if (pie) {
    // Centrado y en su propia línea: pegado a la derecha se metía debajo de la
    // última etiqueta y las dos se leían como una sola frase sin sentido.
    p +=
      `<text x="${(izq + w / 2).toFixed(1)}" y="${alto - 6}" fill="${ROTULO}" ` +
      `font-size="12.5" text-anchor="middle">${escapar(pie)}</text>`;
  }

  return (
    `<svg class="g-vistazo" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `role="img" aria-label="${escapar(pie || "barras")}">${p}</svg>`
  );
}

/* LA LÍNEA Y TU MEDIA. Nada más.
 *
 * Pedido así de explícito: «mi HRV de los últimos meses, sin banda de
 * percentiles ni mitad central, solo la línea y mi media». Las tres cosas que
 * se quitan -la banda de cuartiles, los palos de las salidas y la raya del
 * corte- estaban las tres en la vista de umbral, y las tres obligaban a una
 * leyenda. Un gráfico que necesita leyenda ya perdió, porque la leyenda es el
 * párrafo de la regla 4 escrito en pequeño.
 *
 * `media` llega del servidor y su rótulo también: aquí no se promedia nada.
 */
function lineaConMedia({ puntos, media, rotuloMedia, unidad = "", ancho = 340, alto = 210 }) {
  const ps = (puntos || []).filter((p) => p.valor !== null && p.valor !== undefined);
  if (ps.length < 2 || media === null || media === undefined) return "";

  const izq = 34, der = 10, arr = 22, aba = 34;
  const w = ancho - izq - der, h = alto - arr - aba;

  // Orden de días, no calendario exacto: solo hace falta repartir los puntos a
  // lo largo del eje, y `new Date` sobre una fecha suelta baja un día en España.
  const dnum = (iso) => {
    const [a, m, d] = String(iso).split("-").map(Number);
    return a * 372 + (m - 1) * 31 + d;
  };
  const t0 = dnum(ps[0].fecha);
  const span = dnum(ps[ps.length - 1].fecha) - t0 || 1;
  const x = (iso) => izq + ((dnum(iso) - t0) / span) * w;

  const esc = escalaY([...ps.map((p) => p.valor), media], arr, h, 0.15);
  let p = rejillaY(esc, izq, ancho - der, 5, false);

  // Los meses, abajo, en español corto. Una etiqueta por mes: una por semana
  // llena el eje de texto y ya no se ve la línea, que es lo que se mira.
  const visto = new Set();
  for (const pt of ps) {
    const [a, m] = String(pt.fecha).split("-");
    if (visto.has(`${a}-${m}`)) continue;
    visto.add(`${a}-${m}`);
    const xx = x(pt.fecha);
    if (xx < izq + 4 || xx > ancho - der - 4) continue;
    p +=
      `<line x1="${xx.toFixed(1)}" y1="${arr + h}" x2="${xx.toFixed(1)}" ` +
      `y2="${arr + h + 4}" stroke="${REJILLA}" stroke-width="1"/>` +
      `<text x="${xx.toFixed(1)}" y="${alto - 14}" fill="${ROTULO}" ` +
      `font-size="12" text-anchor="middle">${MESES[Number(m) - 1]}</text>`;
  }

  let camino = "";
  ps.forEach((pt, i) => {
    camino += `${i ? "L" : "M"}${x(pt.fecha).toFixed(1)},${esc.y(pt.valor).toFixed(1)}`;
  });
  p +=
    `<path d="${camino}" fill="none" stroke="${AZUL}" stroke-width="2.4" ` +
    `stroke-linejoin="round" stroke-linecap="round"/>`;

  // La media, rotulada DENTRO del dibujo. Fuera, a la derecha, hacía falta un
  // margen de 46 píxeles que se le quitaban a la línea, y aun así el rótulo se
  // salía por el borde en una pantalla estrecha.
  const ym = esc.y(media);
  const texto = rotuloMedia || `tu media, ${conMenos(num(media, 0))}${unidad ? " " + unidad : ""}`;
  p +=
    `<line x1="${izq}" y1="${ym.toFixed(1)}" x2="${ancho - der}" y2="${ym.toFixed(1)}" ` +
    `stroke="${ROTULO}" stroke-width="1.4" stroke-dasharray="5 4"/>` +
    `<rect x="${ancho - der - 110}" y="${(ym - 21).toFixed(1)}" width="110" ` +
    `height="17" rx="3" fill="#1c2027"/>` +
    `<text x="${ancho - der - 4}" y="${(ym - 8).toFixed(1)}" fill="${ROTULO}" ` +
    `font-size="12.5" text-anchor="end">${escapar(texto)}</text>`;
  // La unidad, ENCIMA de la rejilla y no a la altura de la primera marca. Con
  // `arr + 2` caía en la misma línea que el número de arriba del eje y salía
  // «ms61» pegado, que se lee como una cifra rarísima en vez de como una unidad
  // y un 61. Es el mismo sitio que ocupa en `barrasDeVistazo`, que es donde
  // tenía que haber estado desde el principio.
  if (unidad) {
    p += `<text x="${izq - 32}" y="${arr - 8}" fill="${ROTULO}" font-size="12">${escapar(unidad)}</text>`;
  }

  return (
    `<svg class="g-vistazo" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `role="img" aria-label="${escapar(texto)}">${p}</svg>`
  );
}

/* EL QUESITO, que en realidad es un anillo.
 *
 * También pedido por su nombre: «quesito o barras: mis salidas por tipo, o
 * cuántos días verde/ámbar/rojo llevo». Anillo y no tarta porque el agujero
 * deja sitio para el total, y el total es el denominador: «3 de cada 10» sin
 * saber si son 40 salidas o 4 no dice nada.
 *
 * La leyenda va FUERA, a la derecha. Dentro, en un móvil en vertical, o los
 * rótulos se salen del círculo o hay que encogerlos hasta que no se lean, y un
 * quesito que hay que descifrar es lo contrario de lo que se ha pedido.
 *
 * Los trozos llegan con su color puesto por quien llama. Aquí no se decide
 * ningún color por «bueno o malo»: eso lo prohíbe la cabecera de este archivo y
 * lo prohíbe por un motivo -el mismo verde tendría que significar cosas
 * contrarias según la vista-. El semáforo de días SÍ tiene colores con
 * significado, pero ese significado lo pone el servidor, no el dibujo.
 */
function quesito({ trozos, total, unidadTotal = "", ancho = 340, alto = 190 }) {
  const ts = (trozos || []).filter((t) => t.n > 0);
  if (!ts.length || !total) return "";

  const cx = 78, cy = 92, r = 56, grueso = 28;
  let p = "";
  let ang = -Math.PI / 2;

  for (const t of ts) {
    const barrido = (2 * Math.PI * t.n) / total;
    const x0 = cx + r * Math.cos(ang), y0 = cy + r * Math.sin(ang);
    ang += barrido;
    const x1 = cx + r * Math.cos(ang), y1 = cy + r * Math.sin(ang);
    // Un trozo que da la vuelta entera tiene x0 == x1 y el arco se dibuja de
    // longitud cero: el anillo desaparece justo cuando el reparto es "todo de
    // un tipo", que es un resultado real y perfectamente posible.
    if (ts.length === 1) {
      p +=
        `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" ` +
        `stroke="${t.color}" stroke-width="${grueso}"/>`;
      break;
    }
    p +=
      `<path d="M${x0.toFixed(1)},${y0.toFixed(1)} A${r},${r} 0 ` +
      `${barrido > Math.PI ? 1 : 0} 1 ${x1.toFixed(1)},${y1.toFixed(1)}" ` +
      `fill="none" stroke="${t.color}" stroke-width="${grueso}"/>`;
  }

  /* EL AGUJERO ES REDONDO Y EL RÓTULO ES RECTO, y por ahí se salía.
   *
   * El hueco tiene 42 puntos de radio, o sea 84 de ancho justo en el centro y
   * menos según se baja. «días» cabe; «sesiones juzgadas», que es el
   * denominador de la vista de percepción, mide el doble y se metía debajo del
   * anillo: las dos puntas de la palabra quedaban tapadas por el color. Y el
   * denominador es medio dato -«3 de cada 10» sin saber de qué son los diez no
   * dice nada-, así que taparlo es peor que no ponerlo.
   *
   * Se parte por el primer espacio y va en dos renglones más pequeños. No es una
   * medida del ancho real del texto -para eso haría falta medir la fuente, que
   * en un SVG hecho a mano no se puede-, pero las unidades de este panel son
   * todas de una o dos palabras y con dos renglones caben todas con holgura.
   */
  const corte = unidadTotal.indexOf(" ");
  const lineas = unidadTotal.length <= 9 || corte < 0
    ? [unidadTotal]
    : [unidadTotal.slice(0, corte), unidadTotal.slice(corte + 1)];

  p +=
    `<text x="${cx}" y="${cy - 2}" fill="${TEXTO}" font-size="28" ` +
    `font-weight="700" text-anchor="middle">${escapar(entero(total))}</text>`;
  lineas.filter(Boolean).forEach((linea, i) => {
    p +=
      `<text x="${cx}" y="${cy + 16 + i * 14}" fill="${ROTULO}" font-size="11.5" ` +
      `text-anchor="middle">${escapar(linea)}</text>`;
  });

  // La leyenda se reparte en vertical según cuántos trozos haya. Con dos cabe
  // holgada; con tres -el semáforo- hay que apretar, y apretar es mejor que
  // dejar que el tercero se salga por abajo del viewBox y no se vea.
  const salto = ts.length >= 3 ? 40 : 54;
  let yy = cy - ((ts.length - 1) * salto) / 2 + 6;
  for (const t of ts) {
    p +=
      `<rect x="150" y="${yy - 13}" width="14" height="14" rx="3" fill="${t.color}"/>` +
      `<text x="172" y="${yy}" fill="${TEXTO}" font-size="16">` +
      `${escapar(entero(t.n))} ${escapar(t.etiqueta)}</text>`;
    if (t.sub) {
      p +=
        `<text x="172" y="${yy + 19}" fill="${ROTULO}" font-size="12.5">` +
        `${escapar(t.sub)}</text>`;
    }
    yy += salto;
  }

  const rotulo = ts.map((t) => `${t.n} ${t.etiqueta}`).join(", ");
  return (
    `<svg class="g-vistazo" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `role="img" aria-label="${escapar(`de ${total}: ${rotulo}`)}">${p}</svg>`
  );
}

/* BARRAS TUMBADAS. Las mismas barras de arriba, pero cuando hay que rotular
 * DOCE cosas y los rótulos son «Salida larga (tu cuarto superior)».
 *
 * POR QUÉ NO VALEN LAS VERTICALES. `barrasDeVistazo` reparte el ancho entre las
 * barras: con cuatro tramos salen a ochenta píxeles y la etiqueta cabe en dos
 * líneas cortas. Con doce exposiciones salen a veinticuatro, que no es una
 * barra, es una raya, y la etiqueta debajo habría que girarla. Un rótulo girado
 * es texto que hay que leer con la cabeza torcida: la regla 5 -legible en el
 * móvil, en vertical, sin zoom- se incumple antes de empezar.
 *
 * Tumbadas, el ancho lo gasta el VALOR y el alto lo gasta la lista. Crecer hacia
 * abajo en un móvil es gratis -se llama desplazarse-, y crecer hacia los lados
 * no lo es.
 *
 * LA ETIQUETA VA ENCIMA DE SU BARRA, no a la izquierda en una columna. Una
 * columna de etiquetas se come la mitad del ancho, y esa mitad es justo la que
 * hace que dos barras parecidas se distingan. Encima, cada fila ocupa dos
 * renglones y ninguna palabra se corta.
 *
 * El cero va dibujado por lo mismo que en las verticales, y aquí más: casi todo
 * lo que se mide aquí baja, y sin la raya once barras hacia la izquierda se leen
 * como una lista de longitudes sin signo.
 *
 * Los `rotulos` vienen HECHOS del que llama, igual que en el resto de esta
 * sección: la frontera del cálculo de la cabecera de este archivo no se cruza
 * por ahorrarse un `num()`.
 */
function barrasTumbadas({
  valores, rotulos, etiquetas, pie, positivoEsBueno = true, alReves = null,
  ancho = 340, altoFila = 44,
}) {
  const vs = valores.filter((v) => v !== null && v !== undefined);
  if (!vs.length) return "";

  const arr = 10, aba = pie ? 26 : 8;
  const alto = arr + valores.length * altoFila + aba;
  // El hueco de los dos extremos es para el NÚMERO de la punta. Sin reservarlo,
  // la barra más larga llega al borde y su cifra se sale del `viewBox`: el
  // navegador no la recorta, la dibuja fuera y desaparece. Justo la cifra de la
  // barra más importante.
  const hueco = 46, izq = 6, der = 6;
  const x0 = izq + hueco, x1 = ancho - der - hueco;
  const lo = Math.min(0, ...vs), hi = Math.max(0, ...vs);
  const x = (v) => x0 + ((v - lo) / (hi - lo || 1)) * (x1 - x0);

  // LA LÍNEA DEL CERO VA A TROZOS, uno por barra, y no de arriba abajo de una
  // pieza. El rótulo de cada fila empieza en el margen izquierdo y el cero cae a
  // cincuenta puntos de ahí, así que una línea entera tachaba por la mitad todo
  // nombre que pasara de esa anchura: `cansancio_alto` salía con una raya
  // vertical clavada en medio y parecía texto tachado. Cortada a la altura de
  // las barras se sigue leyendo como una sola línea -los huecos son de diez
  // puntos- y ningún rótulo la cruza.
  const xCero = x(0).toFixed(1);
  let p = "";

  valores.forEach((v, i) => {
    const yFila = arr + i * altoFila;
    p +=
      `<line x1="${xCero}" y1="${yFila + 18}" x2="${xCero}" ` +
      `y2="${yFila + 36}" stroke="${ROTULO}" stroke-width="1.4"/>` +
      `<text x="${izq}" y="${yFila + 13}" fill="${TEXTO}" font-size="13">` +
      `${escapar(etiquetas[i] || "")}</text>`;
    if (v === null || v === undefined) return;
    // `positivoEsBueno` lo pasa quien llama con el `sentido` que mandó el
    // servidor, y no se deduce aquí del signo: subir dos puntos de cansancio y
    // subir dos milisegundos de variabilidad son lo contrario, y el gráfico no
    // tiene forma de saber cuál está mirando. La cabecera de este archivo dice
    // que el color no traduce el número; esto lo cumple repitiendo una decisión
    // ya tomada, igual que la prosa de al lado.
    //
    // `alReves` es lo mismo cuando la decisión NO es la misma para todas las
    // barras. En concordancia cada pareja lleva su propio signo esperado -el
    // cansancio tiene que bajar la variabilidad y el sueño tiene que subirla-,
    // así que un único `positivoEsBueno` pintaría media lista al revés. Cada
    // casilla trae el `al_reves` que calculó el servidor, con TRES estados: va
    // por donde debía, va al contrario, o el número es tan pequeño que su signo
    // es una moneda al aire. El tercero se pinta gris y no azul; un gris es
    // "esto no dice nada", que es exactamente lo que `null` significa ahí.
    const color = alReves
      ? (alReves[i] === false ? AZUL : alReves[i] === true ? NARANJA : TENUE)
      : (v >= 0) === positivoEsBueno ? AZUL : NARANJA;
    const xv = x(v);
    p +=
      `<rect x="${Math.min(x(0), xv).toFixed(1)}" y="${yFila + 20}" ` +
      `width="${Math.max(Math.abs(xv - x(0)), 1.5).toFixed(1)}" height="14" ` +
      `fill="${color}" rx="3"/>` +
      `<text x="${(v >= 0 ? xv + 6 : xv - 6).toFixed(1)}" y="${yFila + 32}" ` +
      `fill="${color}" font-size="14" font-weight="700" ` +
      `text-anchor="${v >= 0 ? "start" : "end"}">` +
      `${escapar(conMenos(rotulos[i]))}</text>`;
  });

  if (pie) {
    p +=
      `<text x="${(ancho / 2).toFixed(1)}" y="${alto - 8}" fill="${ROTULO}" ` +
      `font-size="12.5" text-anchor="middle">${escapar(pie)}</text>`;
  }

  return (
    `<svg class="g-vistazo" viewBox="0 0 ${ancho} ${alto}" width="100%" ` +
    `role="img" aria-label="${escapar(pie || "barras")}">${p}</svg>`
  );
}

/* EL BLOQUE: gráfico grande, UNA frase debajo, y el detalle plegado.
 *
 * Es la regla 1 y la regla 2 convertidas en una sola función, y está aquí para
 * que no se puedan cumplir a medias. Mientras cada vista montaba su `<h2>`, su
 * SVG y su prosa por su cuenta, «el gráfico primero y grande» era una
 * costumbre: se cumplía donde alguien se acordó. Ahora el orden lo decide esta
 * función y no hay forma de colar un párrafo entre el título y el dibujo sin
 * escribir a mano un bloque que se vea distinto del resto.
 *
 * `detalle` es opcional y va SIEMPRE plegado. No lleva `abierto = true` por
 * ningún lado: un detalle que se abre solo es la vista principal otra vez.
 */
/* La mayúscula y el punto de la frase de debajo del gráfico.
 *
 * Las `lectura` del servidor están escritas para ir ENGANCHADAS a algo -«cuando
 * no coinciden es casi siempre porque entrenaste sin que te apeteciera (19 de
 * 28)»- porque llevan desde siempre colgando de un título con dos puntos. Aquí
 * son la única línea de texto del bloque y van solas, así que sin esto la
 * portada abría una frase en minúscula y la cerraba sin punto, y eso en una
 * pantalla que se mira de un vistazo se lee como un texto cortado a medias.
 *
 * Se hace en el molde y no en cada llamada por lo mismo que el orden: la regla
 * «el texto, debajo y en una frase» no puede depender de que quien escriba el
 * bloque siguiente se acuerde. Y se hace aquí y no en el servidor porque la
 * misma `lectura` se sigue usando enganchada dentro de «ver detalle»: cambiarla
 * en origen arreglaría un sitio y rompería el otro. */
function comoFrase(s) {
  const t = String(s || "").trim();
  if (!t) return "";
  const i = t.search(/[\p{L}\p{N}]/u);
  const con = i < 0 ? t : t.slice(0, i) + t[i].toUpperCase() + t.slice(i + 1);
  return /[.!?…:;]$/.test(con) ? con : `${con}.`;
}

function bloqueDeVistazo(titulo, svg, frase, detalle) {
  return (
    `<section class="bloque-vistazo">` +
    `<h2>${escapar(titulo)}</h2>` +
    `<div class="lienzo">${svg}</div>` +
    (frase ? `<p class="frase-vistazo">${escapar(comoFrase(frase))}</p>` : "") +
    (detalle ? plegable("ver detalle", detalle) : "") +
    `</section>`
  );
}
