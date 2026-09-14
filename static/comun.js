/*
 * Lo que comparten el check-in y las métricas.
 *
 * LA REGLA QUE MANDA EN TODA LA PARTE DE MÉTRICAS
 * -----------------------------------------------
 * Esta pantalla NO calcula nada. Ni una media, ni un porcentaje, ni un
 * redondeo que cambie un número. Lo que se pinta es exactamente lo que mandó
 * el servidor, y cuando el servidor manda `null` se pinta el `na` que viene al
 * lado, nunca un hueco ni un cero.
 *
 * No es purismo de arquitectura. El backend tiene tests con datos sintéticos de
 * resultado conocido para cada estadístico; el JavaScript del móvil no tiene
 * ninguno, y un cálculo aquí sería un segundo sitio donde el mismo número puede
 * salir distinto. En una pantalla cuyo trabajo es contrapesar una percepción
 * distorsionada, dos versiones del mismo dato son peores que ninguna.
 *
 * `n_na()` existe para que no haya forma cómoda de saltarse esto: si un valor
 * puede faltar, se pasa por aquí y el motivo se pinta solo.
 */

const $ = (id) => document.getElementById(id);

/* Todo lo que llega del servidor pasa por aquí antes de tocar `innerHTML`. Los
 * motivos de `na` los escribe el backend y son prosa larga con comillas y
 * guiones; no vienen del exterior, pero el día que uno arrastre un `<` el
 * navegador se comería media pantalla sin decir nada. */
function escapar(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fechaLarga(d) {
  return d.toLocaleDateString("es-ES", {
    weekday: "long", day: "numeric", month: "long",
  });
}

/* Un ISO del servidor pintado corto, sin pasar por `new Date(iso)`.
 *
 * `new Date("2026-09-11")` se interpreta como medianoche UTC y en España sale
 * el día 10 por la tarde: la fecha bajaría un día al pintarla. Las fechas de
 * estas vistas son días de calendario que ya decidió el servidor, no instantes,
 * así que se parten a mano y no se tocan. */
const MESES = [
  "ene", "feb", "mar", "abr", "may", "jun",
  "jul", "ago", "sep", "oct", "nov", "dic",
];

function fechaCorta(iso) {
  if (!iso) return "—";
  const [a, m, d] = String(iso).split("-").map(Number);
  if (!a || !m || !d) return String(iso);
  return `${d} ${MESES[m - 1]} ${a}`;
}

function fechaMinima(iso) {
  if (!iso) return "—";
  const [, m, d] = String(iso).split("-").map(Number);
  return `${d}/${m}`;
}

// ---------------------------------------------------------------------------
// Números que pueden faltar
// ---------------------------------------------------------------------------

/* Un número del servidor, o una raya. NUNCA un cero de relleno.
 *
 * `0` y `null` se parecen mucho en JavaScript -los dos son falsy- y la forma
 * corta de escribir esto, `valor || "—"`, convierte un cero de verdad en una
 * raya. En esta pantalla eso es exactamente el fallo que se acaba de barrer del
 * backend, así que la comprobación es explícita contra `null` y `undefined`. */
function num(valor, decimales = 2) {
  if (valor === null || valor === undefined || Number.isNaN(valor)) return "—";
  return Number(valor).toFixed(decimales).replace(".", ",");
}

function pct(valor, decimales = 0) {
  if (valor === null || valor === undefined) return "—";
  return `${Number(valor).toFixed(decimales).replace(".", ",")} %`;
}

function entero(valor) {
  if (valor === null || valor === undefined) return "—";
  return String(valor);
}

/* El bloque de "esto no se puede calcular, y este es el motivo".
 *
 * Es la pieza más importante de la vista entera. La alternativa cómoda -esconder
 * la casilla que no se puede calcular- deja una pantalla que solo enseña lo que
 * salió, y una pantalla que solo enseña lo que salió parece decir más de lo que
 * sabe. Aquí la casilla sigue en su sitio, con el motivo dentro. */
function bloqueNa(motivo) {
  return `<p class="na">${escapar(motivo || SIN_MOTIVO)}</p>`;
}

/* Cuando falta el dato Y falta el motivo. No es lo mismo que no tener dato: es
 * que el backend ha devuelto un hueco sin explicarlo, y eso es un fallo suyo que
 * hay que poder ver desde el móvil en vez de leerlo como "aún no hay datos". */
const SIN_MOTIVO =
  "sin dato y sin motivo: el servidor no ha dicho por qué, y eso es un fallo " +
  "suyo, no una falta de días";

// ---------------------------------------------------------------------------
// Hablar con el servidor
// ---------------------------------------------------------------------------

/* Una respuesta de la API, o un error que se puede leer.
 *
 * Nunca devuelve un objeto vacío ni `{}` de consolación: quien llama recibe los
 * datos o una excepción con el motivo dentro. Una vista pintada con `{}` sale
 * entera en blanco y se lee como "no hay nada que enseñar", que es una respuesta
 * distinta de "no se ha podido preguntar".
 */
async function pedir(ruta, parametros = {}) {
  const url = new URL(ruta, location.origin);
  for (const [k, v] of Object.entries(parametros)) {
    if (v !== null && v !== undefined) url.searchParams.set(k, String(v));
  }

  let r;
  try {
    r = await fetch(url, { cache: "no-store" });
  } catch (err) {
    throw new Error(
      `no se ha podido hablar con el servidor (${err.message}). Estas vistas ` +
      `se calculan enteras ahí: sin conexión no hay nada que enseñar, y ` +
      `enseñar lo de la última vez sería peor.`,
    );
  }

  if (!r.ok) {
    let detalle = `el servidor ha contestado ${r.status}`;
    try {
      const cuerpo = await r.json();
      if (typeof cuerpo.detail === "string") detalle = cuerpo.detail;
      else if (cuerpo.detail) detalle = JSON.stringify(cuerpo.detail);
    } catch { /* el cuerpo no era JSON: se queda el código de estado */ }
    throw new Error(detalle);
  }

  return r.json();
}

// ---------------------------------------------------------------------------
// La navegación
// ---------------------------------------------------------------------------

/* Las siete pantallas, en el orden en que tienen sentido.
 *
 * El check-in va primero porque es lo que se abre a las siete de la mañana. La
 * portada va justo detrás porque es la respuesta a la pregunta que se hace al
 * salir de él -«¿y entonces cómo voy?»- y porque es la única que no obliga a
 * elegir nada para enseñar algo.
 *
 * Las cinco de detalle van después, en el orden en que se leen: primero si lo
 * que noto coincide con el reloj, luego si coincide con retraso, luego qué le
 * hace cada cosa al cuerpo, luego qué ha hecho el motor con todo eso, y al final
 * lo que dicen los números frente a lo que parecía.
 *
 * LAS ETIQUETAS CORTAS NO SON EL NOMBRE TÉCNICO DE LA VISTA. En una barra de
 * siete botones en un móvil caben ocho caracteres, y «Concordancia» ahí no dice
 * nada que ayude a decidir si tocarlo. Dicen QUÉ SE VA A VER: «Coincide»,
 * «Retraso», «Efecto», «Motor», «Real». El nombre largo y técnico sigue entero
 * en el título de la pantalla, que es donde hay sitio para explicarlo.
 */
const PANTALLAS = [
  { href: "/", etiqueta: "Check-in", corta: "Hoy" },
  { href: "/metricas.html#portada", etiqueta: "Cómo vas", corta: "Cómo vas" },
  { href: "/metricas.html#concordancia", etiqueta: "Concordancia", corta: "Coincide" },
  { href: "/metricas.html#desfase", etiqueta: "Desfase", corta: "Retraso" },
  { href: "/metricas.html#impacto", etiqueta: "Impacto", corta: "Efecto" },
  { href: "/metricas.html#auditoria", etiqueta: "Auditoría", corta: "Motor" },
  { href: "/metricas.html#percepcion", etiqueta: "Percepción", corta: "Real" },
];

/* La barra de abajo, pintada desde `PANTALLAS` y no escrita a mano en los dos
 * HTML. Escrita dos veces, añadir una vista dejaría media aplicación sin enlace
 * a ella y nadie se enteraría hasta buscarla. */
function pintarNav(activa) {
  const nav = $("nav");
  if (!nav) return;
  nav.innerHTML = PANTALLAS.map((p) => {
    const sel = p.href === activa || (activa && p.href.endsWith(activa));
    return (
      `<a href="${escapar(p.href)}"${sel ? ' class="activa" aria-current="page"' : ""}>` +
      `${escapar(p.corta)}</a>`
    );
  }).join("");
}

// ---------------------------------------------------------------------------
// La cobertura
// ---------------------------------------------------------------------------

const FUENTES = {
  checkin: "check-ins",
  garmin: "datos de Garmin",
  bici: "salidas de bici",
  fuerza: "entrenos de fuerza",
};

/* De dónde sale una SERIE, que no es el mismo vocabulario que el de arriba.
 *
 * `FUENTES` son las cuatro patas de la cobertura -lo que hay y lo que falta en
 * la ventana-; esto son los tres orígenes que declara `Definicion.fuente` en
 * `series.py`, y no coinciden: allí «bici» y «fuerza» son dos fuentes distintas
 * y aquí las dos son `entreno`. Se parecen lo bastante como para que juntarlas
 * pareciera buena idea, y lo bastante poco como para que `FUENTES["entreno"]`
 * diera `undefined` y la ficha de las series de entreno saliera empezando por
 * «undefined ·». Dos vocabularios distintos, dos mapas distintos, y cada uno
 * dice de cuál es.
 *
 * Igual que `METODOS`: la clave que no esté se imprime tal cual. Fea y visible
 * antes que ausente. */
const ORIGENES = {
  checkin: "lo contestas tú",
  garmin: "lo mide el reloj",
  entreno: "sale de lo entrenado",
};

/* Desde cuándo hay datos de cada cosa, y de qué NO hay ninguno.
 *
 * Va arriba del todo de cada vista y no escondido en un pie, porque es lo que
 * decide cómo hay que leer todo lo demás. Una correlación calculada sobre doce
 * días y otra sobre ciento setenta se pintan igual de grandes; lo que las
 * distingue es esta línea.
 *
 * Las fuentes con ventana `null` se nombran una a una. Es la diferencia entre
 * "de esto no hay nada" y "esto no lo miramos", y sin decirlo la vista parece
 * completa cuando le falta una pata entera.
 *
 * `cob` a `null` NO es lo mismo que un `cob` con las cuatro fuentes vacías, y
 * por eso se trata aparte en vez de caer en el mismo camino. La vista de
 * percepción no calcula cobertura -trabaja sobre `session_performance`, que ya
 * es el resultado de cruzar las fuentes-, así que pintarle "sin ningún dato de
 * check-ins, Garmin, bici ni fuerza" sería afirmar un vacío que nadie ha
 * mirado. Se dice lo único que se sabe: la ventana, y que esta vista no reporta
 * cobertura.
 */
function pintarCobertura(cob, ventana) {
  // «Ventana pedida» es el nombre del parámetro, no el nombre de la cosa. Lo que
  // esta línea dice es qué trozo de calendario está mirando la pantalla.
  const cabecera =
    `<p class="ventana">Se está mirando del <b>${fechaCorta(ventana.desde)}</b> ` +
    `al <b>${fechaCorta(ventana.hasta)}</b> · ${entero(ventana.dias)} días</p>`;

  if (cob === null || cob === undefined) {
    return (
      `<section class="cobertura">${cabecera}` +
      `<p class="ficha">Esta vista no mira las fuentes una a una: trabaja sobre ` +
      `las sesiones ya cruzadas, y cada una lleva dentro de qué pudo juzgarse.</p>` +
      `</section>`
    );
  }

  const trozos = [];
  const vacias = [];

  /* `{desde, hasta}`, que es lo que manda `Cobertura.como_dict`, y NO un par
   * `[desde, hasta]`.
   *
   * Aquí ponía `v.length === 2`. Un objeto no tiene `length`, así que la
   * comprobación daba `false` siempre y las cuatro fuentes caían en `vacias`:
   * la pantalla abría diciendo "Sin ningún dato en esta ventana de: check-ins,
   * datos de Garmin, salidas de bici, entrenos de fuerza" encima de ciento
   * setenta y nueve días de Garmin.
   *
   * No daba ningún error ni escribía ningún `undefined`: elegía la rama
   * equivocada y afirmaba, con una frase bien escrita, lo contrario de lo que
   * pasaba. Y lo afirmaba justo en la línea que existe para decir cómo hay que
   * leer todo lo demás. Por eso se comprueban las dos claves por su nombre en
   * vez de mirar la forma: si el backend cambia el contrato, lo que sale es el
   * aviso de que no hay datos de esa fuente, que es falso pero visible, y no un
   * `undefined → undefined` que al menos se vería. Que esto pueda volver a
   * pasar en silencio es la razón de `test_la_cobertura_de_la_pwa_lee_el_mismo
   * _contrato_que_escribe_el_backend`.
   */
  for (const [clave, nombre] of Object.entries(FUENTES)) {
    const v = cob[clave];
    if (v && v.desde && v.hasta) {
      trozos.push(
        `<span><b>${escapar(nombre)}</b> ${fechaCorta(v.desde)} → ` +
        `${fechaCorta(v.hasta)}</span>`,
      );
    } else {
      vacias.push(nombre);
    }
  }

  const cuerpo = trozos.length
    ? `<div class="fuentes">${trozos.join("")}</div>`
    : "";

  const aviso = vacias.length
    ? `<p class="na">Sin ningún dato en esta ventana de: ${escapar(vacias.join(", "))}. ` +
      `Todo lo que dependa de esas fuentes sale sin calcular, y con el motivo escrito.</p>`
    : "";

  return `<section class="cobertura">${cabecera}${cuerpo}${aviso}</section>`;
}

// ---------------------------------------------------------------------------
// Piezas de presentación que se repiten
// ---------------------------------------------------------------------------

/* «4 día(s)», catorce veces en la misma pantalla.
 *
 * El paréntesis es el atajo de quien no quiere decidir, y decide igual: decide
 * que la pantalla parezca a medio terminar. En un panel que existe para que un
 * hallazgo se pueda leer como una frase, «Hay 2 hallazgo(s) más» es la única
 * línea que delata que debajo hay una plantilla.
 *
 * `n` puede llegar como cadena desde el payload -y `"1" === 1` es falso-, así
 * que se convierte antes de comparar. Y se compara el VALOR ABSOLUTO: los
 * desfases van de −3 a +3, y «−1 días» sería lo mismo otra vez pero al revés.
 *
 * El plural se pasa entero en vez de añadirle una «s»: en castellano no
 * funciona -sesión/sesiones- y una regla que acierta en cuatro casos de cinco
 * es peor que ninguna, porque el que falla no se ve venir. */
function plural(n, uno, varios) {
  return Math.abs(Number(n)) === 1 ? uno : varios;
}

/* El número y su palabra ya concordados: `cuenta(1, "día", "días")` → "1 día". */
function cuenta(n, uno, varios) {
  return `${entero(n)} ${plural(n, uno, varios)}`;
}

/* Cómo se dice en castellano el nombre del método.
 *
 * `spearman` y `pearson` son las claves del contrato: viajan en la query, las
 * valida `_metodo` en `api.py` y las guarda `Resultado.metodo`. Lo que NO son es
 * texto para leer. Estaban saliendo tal cual en la ficha de cada casilla, en
 * minúsculas y sin traducir, que es una palabra en inglés dentro de una frase en
 * castellano y encima el apellido de un estadístico muerto.
 *
 * Lo que hay que saber al leer la línea no es de quién es el método, es sobre
 * qué se calculó: «de rangos» significa que solo se ha mirado el ORDEN de los
 * días -quién fue mejor que quién- y no la distancia entre ellos, y por eso un
 * día rarísimo no arrastra el resultado. «Lineal» significa lo contrario.
 *
 * La clave que no esté en el mapa se imprime tal cual y no desaparece. Si un día
 * el servidor manda un tercer método, la ficha dirá su nombre técnico -feo, pero
 * visible- en vez de dejar el hueco en blanco y hacer creer que no hubo método.
 */
const METODOS = {
  spearman: "correlación de rangos",
  pearson: "correlación lineal",
};

/* `43 pares comparados · correlación de rangos · 12 descartados`. La ficha
 * técnica de un número.
 *
 * Se pidió que TODA métrica exponga su n y su ventana, y este es el sitio por el
 * que pasan todas. Los descartados solo se nombran cuando los hay: un "0
 * descartados" en cada casilla es ruido que enseña a no leer la línea.
 *
 * El `n = 43` de antes decía la verdad en un idioma que hay que haber estudiado.
 * `n` es la letra con la que se llama a esto en un paper; lo que significa aquí
 * es cuántas veces se han podido poner dos datos uno al lado del otro, y eso es
 * exactamente lo que hay que saber para decidir si el número de arriba vale
 * algo. Se dice entero: la ficha es el sitio donde los números viven, pero
 * escritos, no cifrados. */
function ficha(c) {
  const trozos = [`${entero(c.n)} pares comparados`];
  if (c.metodo) trozos.push(escapar(METODOS[c.metodo] || c.metodo));
  if (c.descartados) trozos.push(`${entero(c.descartados)} descartados`);
  if (c.desde && c.hasta) {
    trozos.push(`${fechaMinima(c.desde)}–${fechaMinima(c.hasta)}`);
  }
  return `<p class="ficha">${trozos.join(" · ")}</p>`;
}

/* El detalle que se abre tocando. En un móvil no cabe todo a la vez, y la
 * alternativa -recortar- es la que deja fuera justo el motivo de lo que no se
 * pudo calcular. Plegado sigue estando; recortado, no. */
function plegable(titulo, contenido, abierto = false) {
  return (
    `<details class="pliegue"${abierto ? " open" : ""}>` +
    `<summary>${escapar(titulo)}</summary>${contenido}</details>`
  );
}
