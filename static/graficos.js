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
 * El tercero es concordancia y desfase: siete parejas declaradas de antemano,
 * cada una con el signo que se espera escrito debajo, que es una pregunta
 * distinta de rastrear una rejilla a ver qué sale. Por eso la línea "aguanta la
 * corrección" solo aparece donde de verdad hubo corrección: escribirla en las
 * cinco vistas la convertiría en decoración.
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
function barrasSemanales(semanas, alto = 64) {
  if (!semanas || !semanas.length) return "";

  const anchoBarra = 10, hueco = 3;
  const piezas = [];

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
        `${s.green} verde, ${s.amber} ámbar, ${s.red} rojo, ` +
        `${s.sin_decision} sin decisión`,
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
