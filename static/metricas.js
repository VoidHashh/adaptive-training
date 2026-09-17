/*
 * Las cinco vistas de métricas.
 *
 * QUÉ HACE ESTE ARCHIVO Y QUÉ NO HACE
 * -----------------------------------
 * Pide, ordena y pinta. No calcula. Cada `r`, cada media, cada porcentaje y cada
 * `n` sale tal cual del payload; cuando el payload trae `null` se pinta el `na`
 * que viene pegado a ese `null`, y nunca un hueco.
 *
 * Es la misma regla del formulario de la mañana llevada a su extremo lógico. Allí
 * el fallo posible era enviar un 5 que nadie había contestado; aquí es enseñar un
 * 0,0 que nadie ha medido. En una pantalla que existe para contrapesar una
 * percepción distorsionada, un número inventado no es un bug de presentación: es
 * lo contrario exacto de para lo que se hizo.
 *
 * NINGUNA VISTA SE ESCONDE
 * ------------------------
 * Con la base vacía se pintan las cinco, enteras, con el motivo en cada casilla.
 * No hay "esto lo verás más adelante": eso convierte la falta de datos en una
 * decisión de producto invisible, y la falta de datos es justo lo que hay que
 * poder ver -cuántos días faltan y de qué-.
 *
 * LAS LISTAS SALEN DEL PAYLOAD
 * ----------------------------
 * Ni un deslizador, ni una exposición, ni una regla escritos a mano aquí. Todo
 * lo que se puede elegir se rellena de lo que manda el servidor. Escrito a mano,
 * añadir una señal al `config.yaml` la dejaría fuera de las métricas sin un solo
 * error, y esa es la familia de fallos que este proyecto lleva meses cazando.
 */

const RUTAS = {
  portada: "/api/metrics/portada",
  concordancia: "/api/metrics/concordancia",
  desfase: "/api/metrics/desfase",
  impacto: "/api/metrics/impacto",
  ranking: "/api/metrics/ranking-ejercicios",
  auditoria: "/api/metrics/auditoria",
  percepcion: "/api/metrics/percepcion",
  umbral: "/api/metrics/umbral",
};

/* LOS TÍTULOS SON LO QUE SE VE, NO CÓMO SE LLAMA LA VISTA POR DENTRO.
 *
 * Aquí ponía «Concordancia», «Desfase», «Auditoría» y «Percepción»: los cinco
 * nombres con los que se pidieron las vistas, que son los nombres correctos para
 * hablar de ellas y los peores posibles para encontrarlas. A las siete de la
 * mañana, «Desfase» no dice si lo que hay detrás sirve para lo que se está
 * buscando; «¿Se adelanta o va con retraso?» sí.
 *
 * Los nombres técnicos no se han perdido: siguen siendo las claves de este
 * objeto, las anclas de la URL (`#desfase`) y los nombres de los módulos del
 * servidor. Lo que ha cambiado es que ya no son lo que se lee.
 *
 * El subtítulo NO repite el título ni la pregunta del encabezado. Dice el
 * MÉTODO -sobre qué se calcula, con qué ventana, contra qué-, que es lo único
 * que no cabe en ninguno de los otros dos y es lo que decide si un número se
 * puede creer.
 */
const VISTAS = {
  portada: {
    titulo: "Cómo vas",
    subtitulo: "Lo que hay que saber hoy, sin tener que buscarlo",
    pintar: pintarPortada,
  },
  concordancia: {
    titulo: "Lo que notas y lo que mide el reloj",
    subtitulo: "Tus check-ins cruzados con el reloj, el mismo día",
    pintar: pintarConcordancia,
  },
  desfase: {
    titulo: "¿Se adelanta o va con retraso?",
    subtitulo: "La misma pregunta, de tres días antes a tres días después",
    pintar: pintarDesfase,
  },
  impacto: {
    titulo: "Qué efecto tiene cada cosa",
    subtitulo: "Uno, dos y tres días después de cada entreno y cada salida",
    pintar: pintarImpacto,
  },
  auditoria: {
    titulo: "Qué ha hecho el motor",
    subtitulo: "Qué decidió cada día, con qué regla y con qué datos",
    pintar: pintarAuditoria,
  },
  percepcion: {
    titulo: "Lo que esperabas y lo que salió",
    subtitulo: "Sesión a sesión, la mañana frente al resultado",
    pintar: pintarPercepcion,
  },
  // El título es la pregunta de verdad y no «El umbral de la bici», que es como
  // se pidió la vista: «umbral» es la palabra del hallazgo, no la de la duda que
  // lleva a abrir esta pantalla. El menú de abajo sí la lleva -ahí hace falta
  // una etiqueta corta y reconocible-, y aquí arriba, con sitio para una frase,
  // gana la frase.
  umbral: {
    titulo: "Cuánta bici te pasa factura",
    subtitulo: "Tus salidas partidas por carga, contra la HRV del día siguiente",
    pintar: pintarUmbral,
  },
};

/* La portada es lo primero que se ve, y eso es la mitad del rediseño.
 *
 * Antes se entraba en Concordancia: catorce correlaciones con su r, su p y su
 * corrección por comparaciones múltiples. Es la vista más técnica de las seis y
 * era la puerta de entrada. Para saber si hoy estás bien había que elegir
 * variables en un desplegable y saber leer un coeficiente, o sea que la
 * respuesta a la pregunta más frecuente costaba más trabajo que cualquier otra.
 *
 * Ahora se entra en la portada: tres bloques, frases, y los números debajo para
 * quien quiera bajar. Las otras cinco vistas siguen enteras y sin tocar -no se
 * ha simplificado nada, se ha ordenado- y se llega a ellas desde abajo.
 */
const POR_DEFECTO = "portada";
const RECUERDA_VENTANA = "metricas-dias";

// Lo elegido en los desplegables de dentro de una vista. Vive aquí y no en el
// DOM para que cambiar la ventana no lo pierda: al recargar los datos se vuelve
// a pintar todo, y sin esto el filtro saltaría solo a su valor inicial.
const elegido = { respuesta: null };

/* Los nombres cortos de las piezas del índice, tal como los manda el servidor en
 * `componentes`. Se apuntan al pintar la vista 5 porque las tarjetas de sesión
 * los necesitan y no los reciben: llegan hasta ahí a través de una lista dentro
 * de un plegable, y hacerlos bajar por ese camino sería pasar el mismo
 * diccionario por cuatro funciones que no lo usan.
 *
 * Vacío hasta que llega el payload, y entonces se pinta la clave cruda. Fea,
 * pero cierta: lo que no se hace es tener aquí una segunda lista de nombres
 * escrita a mano que se quede vieja el día que se añada una pieza. */
const nombresCortos = {};

// ---------------------------------------------------------------------------
// El enrutador
// ---------------------------------------------------------------------------

function vistaActual() {
  const h = (location.hash || "").replace(/^#/, "");
  return VISTAS[h] ? h : POR_DEFECTO;
}

async function cargar() {
  const clave = vistaActual();
  const v = VISTAS[clave];
  const dias = Number($("dias").value);

  $("titulo").textContent = v.titulo;
  $("subtitulo").textContent = v.subtitulo;
  // Y el título de la pestaña con él. El `<title>` de `metricas.html` dice
  // "Cómo vas", que acierta SOLO porque la portada es la vista por defecto:
  // en cuanto se cambia de vista, la pestaña sigue anunciando la portada
  // mientras la pantalla enseña el motor. En el escritorio es una pestaña mal
  // rotulada; en el móvil es el nombre con el que la PWA aparece en el
  // conmutador de aplicaciones y el que se propone al guardarla en la pantalla
  // de inicio, así que el rótulo equivocado es el que se queda.
  document.title = v.titulo;
  pintarNav(`#${clave}`);

  $("vista").hidden = true;
  $("cargando").hidden = false;
  $("cargando").className = "cargando";
  $("cargando").textContent = "Calculando en el servidor…";

  try {
    await v.pintar(dias);
  } catch (err) {
    // El motivo entero, sin resumir. Un "error al cargar" obliga a abrir el
    // portátil para saber si es que no hay red, si falta una clave del
    // `config.yaml` o si la ventana pedida no vale.
    $("cargando").className = "cargando mal";
    $("cargando").innerHTML =
      `<strong>No se ha podido cargar esta vista.</strong><br>` +
      `${escapar(err.message)}`;
    $("vista").hidden = true;
    return;
  }

  $("cargando").hidden = true;
  $("vista").hidden = false;
}

// ---------------------------------------------------------------------------
// El encabezado de estado, en todas las vistas
// ---------------------------------------------------------------------------

/* Qué contesta esta vista, y en qué estado está para contestarlo.
 *
 * EL SERVIDOR LLEVABA MESES MANDÁNDOLO Y NADIE LO PINTABA. Las seis vistas
 * traen `encabezado` en el payload -con la pregunta, el estado, el resumen y el
 * recuento en la moneda de cada vista- y el navegador lo tiraba entero. Es el
 * mismo fallo que el aviso del `config.yaml` en la pantalla de la mañana:
 * calculado, servido, publicado y sin un solo lector.
 *
 * Y AQUÍ IMPORTA MÁS QUE EN NINGÚN SITIO, porque una vista de métricas sin
 * estado se lee siempre igual de fiable. Concordancia con cero pares y
 * Concordancia con los catorce pintan las dos una pantalla con pinta de
 * pantalla llena; la diferencia -que una no puede contestar nada todavía- vive
 * entera en este bloque. Sin él hay que deducir la cobertura contando tarjetas.
 *
 * El estado NO se recalcula aquí: `vacio`, `parcial` y `con_datos` los decide
 * el servidor contando lo que la vista acaba de devolver. Aquí solo se elige el
 * color.
 */
const CLASE_ESTADO = {
  vacio: "mal",
  parcial: "ojo",
  con_datos: "bien",
};

const ROTULO_ESTADO = {
  vacio: "Todavía no se puede contestar",
  parcial: "Se puede contestar a medias",
  con_datos: "Con datos suficientes",
};

function encabezadoVista(e) {
  if (!e) return "";
  const clase = CLASE_ESTADO[e.estado] || "ojo";
  const rotulo = ROTULO_ESTADO[e.estado] || e.estado;

  // El recuento va en la moneda de la vista y con su denominador, porque es lo
  // que dice cuánto de lo que la vista sabe mirar está mirando de verdad. «135
  // de 396» y «135» son dos frases muy distintas.
  const cuenta =
    e.n !== null && e.n !== undefined && e.de
      ? `<span class="cuenta-estado">${entero(e.n)} de ${entero(e.de)}</span>`
      : "";

  return (
    `<section class="encabezado-vista ${clase}">` +
    `<p class="pregunta">${escapar(e.pregunta)}</p>` +
    `<p class="estado"><span class="rotulo">${escapar(rotulo)}</span>${cuenta}</p>` +
    `<p class="resumen-estado">${escapar(e.resumen)}</p>` +
    `</section>`
  );
}

// ---------------------------------------------------------------------------
// Vista 0: la portada
// ---------------------------------------------------------------------------

/* Los tres bloques, en el orden en el que se responden las preguntas de verdad.
 *
 * «Cómo voy» es el estado de hoy; «Qué ha cambiado» es la única comparación que
 * se puede hacer sin elegir nada; «Lo que ya sé de ti» es lo que el sistema ha
 * aprendido y no cambia de un día para otro. De arriba abajo va de lo más
 * volátil a lo más estable, que es también de lo más urgente a lo más
 * interesante.
 *
 * NO HAY UN SOLO NÚMERO CALCULADO AQUÍ. Las frases, los niveles, las lecturas y
 * los `na` vienen hechos del servidor. Esta función elige el orden y el color, y
 * nada más: la regla de todo el archivo, que en la portada importa el doble
 * porque es la pantalla que más se va a mirar y la que menos contexto enseña.
 */
async function pintarPortada(dias) {
  const d = await pedir(RUTAS.portada, { dias });

  // La portada NO lleva encabezado de estado, y es la única. Las otras cinco lo
  // llevan porque contestan una pregunta concreta y hay que saber si pueden
  // contestarla; la portada es su propio encabezado de arriba abajo -cada uno de
  // los tres bloques trae su estado y su motivo dentro-, y ponerle otro encima
  // sería un estado resumen de tres cosas que no comparten moneda.
  const partes = [pintarCobertura(d.cobertura, d.ventana)];

  partes.push(bloqueComoVoy(d.como_voy));
  partes.push(bloqueQueHaCambiado(d.que_ha_cambiado));
  partes.push(bloqueLoQueSeSabe(d.lo_que_se_sabe));
  partes.push(bloqueLoQueFalta(d.lo_que_no_se_puede_saber));

  $("vista").innerHTML = partes.join("");
}

/* Lo que TODAVÍA NO se puede contestar, y qué dato exacto lo abriría.
 *
 * Va el último y va pequeño, pero va. Es lo que convierte «el panel está vacío»
 * en «el panel te está diciendo por qué está vacío», y sin él las cuatro vistas
 * sin datos son indistinguibles de cuatro vistas rotas.
 *
 * LA SECCIÓN QUE EXPLICA LOS VACÍOS NO PUEDE VACIARSE EN SILENCIO.
 *
 * Aquí ponía, y era la única excepción declarada de todo el panel: «cuando no
 * falta nada NO se pinta nada, porque un "no falta nada" permanente es una línea
 * que se deja de leer a la semana». El miedo se entiende y en parte es cierto,
 * pero la conclusión estaba del revés: el bloque cuyo trabajo es distinguir
 * «aquí no pasa nada» de «aquí falta un dato» desaparecía sin decir cuál de las
 * dos cosas era. Justo el fallo del que protege, cometido por él.
 *
 * Y lo que se escribe en el hueco no es «no falta nada», que efectivamente no
 * informa. Es la otra mitad: que a partir de aquí una vista vacía YA NO es por
 * falta de histórico, y por tanto hay que mirar otra cosa. Eso es una noticia y
 * no se puede deducir de una ausencia.
 *
 * Es el mismo criterio que `bloqueNa` aplica en las otras cinco vistas.
 */
function bloqueLoQueFalta(lista) {
  const cabecera = `<h2 class="grupo">Lo que todavía no se puede contestar</h2>`;
  if (!lista || !lista.length) {
    return (
      cabecera +
      `<p class="explica">Nada: las cuatro preguntas de esta pantalla ya tienen ` +
      `datos suficientes detrás. Si alguna vista sale vacía a partir de aquí, ` +
      `no es por falta de histórico.</p>`
    );
  }
  return (
    cabecera +
    `<p class="explica">No es que esté roto: es que le falta un dato concreto, y ` +
    `aquí está cuál.</p>` +
    lista.map((f) => (
      `<article class="tarjeta fina">` +
      `<h3>${escapar(f.que)}</h3>` +
      `<p class="lectura">${escapar(f.falta)}</p>` +
      `<p class="ficha">Abriría: ${f.vistas.map((v) => (
        `<a href="#${escapar(v)}">${escapar(VISTAS[v] ? VISTAS[v].titulo : v)}</a>`
      )).join(" · ")}</p>` +
      `</article>`
    )).join("")
  );
}

/* El color de una línea sale de `valencia`, que lo decide el servidor.
 *
 * Y lo decide él porque "alto" no significa "bien" en todas las señales: una
 * HRV alta es buena y una frecuencia en reposo alta es mala. Traducir el nivel
 * a un color aquí obligaría a tener en el navegador una segunda tabla de qué
 * señal va en qué dirección, que es exactamente la que se quedaría vieja el día
 * que se añada una señal nueva al `config.yaml`. El servidor ya sabe el sentido
 * de cada una; aquí solo se pinta lo que diga.
 */
const CLASE_VALENCIA = {
  mejor: "bien",
  peor: "mal",
  normal: "neutro",
  neutro: "neutro",
};

function cabeceraBloque(b) {
  return (
    `<h2 class="grupo">${escapar(b.titulo)}</h2>` +
    (b.subtitulo ? `<p class="explica">${escapar(b.subtitulo)}</p>` : "")
  );
}

/* Las cuatro casillas de «¿te apetece?» contra «¿vas a entrenar?».
 *
 * Esto NO es decoración de la línea de discordancia: es la mitad de la línea. El
 * número solo -«el 30 % de los días no coincidieron»- junta dos cosas opuestas,
 * «me apetecía y no fui» y «no me apetecía y fui», y los dos repartos extremos
 * dan exactamente el mismo 30 % describiendo a dos personas distintas. Por eso
 * el servidor manda `tabla` pegada al número y por eso aquí se pinta siempre que
 * venga: si algún día alguien decide que ocupa mucho y la mete detrás de un
 * plegable, lo que queda a la vista es el dato que no se puede leer solo.
 *
 * LAS CUATRO SIEMPRE, INCLUIDAS LAS DE CERO. Una casilla vacía que no se pinta
 * se lee como «esto no me pasa»; pintada con su total al lado se lee como lo que
 * es. Es el mismo cuidado que `num()` tiene aquí al lado con el cero contra la
 * raya, aplicado a un recuento.
 *
 * Y el orden es el que manda el servidor, sin tocar. Ordenarlo aquí -las más
 * llenas primero, por ejemplo- haría que la casilla de arriba a la izquierda
 * cambiara de significado según el mes, y una tabla de dos por dos que se
 * reordena sola no se puede comparar consigo misma de un día para otro.
 */
function tablaDiscordancia(t) {
  if (!t) return "";
  if (t.na) {
    return (
      `<div class="casillas">` +
      `<p class="explica">${escapar(t.titulo)}</p>` +
      bloqueNa(t.na) +
      `<p class="ficha">${escapar(t.ficha)}</p></div>`
    );
  }

  const filas = t.celdas.map((c) => (
    // `discordante` lo decide el servidor, igual que `valencia`. Recalcularlo
    // aquí con un `c.apetece !== c.voy` sería tener en el navegador una segunda
    // copia de la regla, que es la que se quedaría vieja.
    `<tr class="${c.discordante ? "discorde" : ""}">` +
    `<td>${escapar(c.etiqueta)}</td>` +
    `<td>${entero(c.n)}</td>` +
    `<td>${pct(c.pct, 1)}</td></tr>`
  )).join("");

  return (
    `<div class="casillas">` +
    `<p class="explica">${escapar(t.titulo)}</p>` +
    (t.aviso ? `<p class="aviso tenue">${escapar(t.aviso)}</p>` : "") +
    `<table class="tabla"><thead><tr><th>Lo que pasó</th><th>Días</th>` +
    `<th>%</th></tr></thead><tbody>${filas}</tbody></table>` +
    (t.lectura ? `<p class="explica">${escapar(t.lectura)}</p>` : "") +
    `<p class="ficha">${escapar(t.ficha)}</p></div>`
  );
}

function bloqueComoVoy(b) {
  if (!b) return "";
  if (b.estado === "vacio" || !b.lineas || !b.lineas.length) {
    return cabeceraBloque(b) + bloqueNa(b.na);
  }

  const filas = b.lineas.map((l) => {
    // Una señal sin datos NO se esconde: se pinta con su motivo. Esconderla
    // convertiría "todavía no hay fuerza apuntada" en "la fuerza va bien", que
    // es la diferencia entre no saber y creer que se sabe.
    if (l.na) {
      return (
        `<div class="linea-portada sin-dato">` +
        `<div class="etiqueta-portada">${escapar(l.etiqueta)}</div>` +
        `<div class="lectura-portada">${escapar(l.na)}</div>` +
        // La tabla va TAMBIÉN aquí, y no es simetría por simetría: el `na` de
        // esta línea dice que la última SEMANA no se puede situar dentro del
        // histórico, y la tabla no habla de la semana, habla de la ventana
        // entera. El servidor la manda por las tres salidas a propósito -está
        // escrito en el docstring de `_linea_de_serie`- y este `return` la
        // tiraba. Se veía una portada perfecta a la que le faltaba lo pedido.
        tablaDiscordancia(l.tabla) +
        `</div>`
      );
    }
    const clase = CLASE_VALENCIA[l.valencia] || "neutro";
    // El número y el percentil van en la ficha, debajo y en pequeño: son la
    // comprobación, no el mensaje. El mensaje es la lectura en castellano.
    const detalle = [];
    if (l.media !== null && l.media !== undefined) {
      detalle.push(`${num(l.media)}${l.unidad ? ` ${escapar(l.unidad)}` : ""}`);
    }
    /* «Percentil 68 de lo tuyo» decía la verdad y hacía falta saber qué es un
     * percentil para leerla; y es la PRIMERA pantalla, la que se mira a las
     * siete de la mañana sin haberse despertado del todo. «Por encima del 68 %
     * de tus días» es lo mismo, palabra por palabra, sin tener que saber nada.
     *
     * Lo que NO se ha tocado es contra qué se compara: sigue siendo el histórico
     * propio y no una tabla de valores normales, que es la decisión que se tomó
     * para todo el proyecto. La frase lo dice más claro que antes -«tus días»-,
     * no menos. */
    if (l.percentil !== null && l.percentil !== undefined) {
      detalle.push(`por encima del ${entero(l.percentil)} % de tus días`);
    }
    if (l.n_reciente) detalle.push(`medido en ${cuenta(l.n_reciente, "día", "días")}`);

    return (
      `<div class="linea-portada">` +
      `<div class="etiqueta-portada">${escapar(l.etiqueta)}</div>` +
      `<div class="lectura-portada ${clase}">${escapar(l.lectura)}</div>` +
      // La barra va ENTRE la lectura y la ficha, y las dos cosas importan. Entre
      // la lectura porque el dibujo apoya al veredicto y no al revés; y encima
      // de la ficha porque la ficha ES su lectura -«por encima del 10 % de tus
      // días»- y la regla de `graficos.js` es que ningún dibujo se pinta solo.
      // Pegadas, se leen como una; separadas serían un adorno y un número.
      barraPercentil(l) +
      (detalle.length
        ? `<div class="ficha-portada">${escapar(detalle.join(" · "))}</div>`
        : "") +
      // Once de las doce líneas no la traen, y la que la trae no se entiende sin
      // ella. Va DENTRO de la línea y no al final del bloque para que se lea
      // pegada a su número y no como una tabla suelta al pie de la portada.
      tablaDiscordancia(l.tabla) +
      `</div>`
    );
  }).join("");

  return cabeceraBloque(b) + `<article class="tarjeta portada">${filas}</article>`;
}

function bloqueQueHaCambiado(b) {
  if (!b) return "";
  if (b.estado === "vacio" || !b.lineas || !b.lineas.length) {
    return cabeceraBloque(b) + bloqueNa(b.na);
  }

  const filas = b.lineas.map((l) => (
    `<div class="linea-portada">` +
    `<div class="etiqueta-portada">${escapar(l.etiqueta)}</div>` +
    `<div class="lectura-portada">${escapar(l.lectura)}</div>` +
    `</div>`
  )).join("");

  return cabeceraBloque(b) + `<article class="tarjeta portada">${filas}</article>`;
}

/* EL FEED DE HALLAZGOS, que es lo que este proyecto existe para producir.
 *
 * La frase va primera, grande y en castellano; los números van debajo en la
 * ficha. Ese orden es todo el punto: «las salidas largas te suben el pulso en
 * reposo al día siguiente» es el hallazgo, y `r = 0,39 · p = 0,0001 · n = 178`
 * es cómo se sabe. Puesto al revés -que es como estaba en todas las demás
 * vistas- hay que traducir mentalmente antes de poder leer nada.
 *
 * LA CONFIRMACIÓN Y LA DISCREPANCIA VAN CON EL HALLAZGO, no en un apéndice. Que
 * lo mismo salga por cuatro caminos distintos es parte de cuánto fiarse, y
 * enterrarlo en un plegable deja la frase sola y pareciendo más segura de lo que
 * es. Lo mismo, en el otro sentido, con lo que discrepa.
 */
function bloqueLoQueSeSabe(b) {
  if (!b) return "";
  if (b.estado === "vacio" || !b.portada || !b.portada.length) {
    return cabeceraBloque(b) + bloqueNa(b.na);
  }

  const tarjetas = b.portada.map(tarjetaHallazgo).join("");

  // El recuento y la nota sobre el azar, debajo del feed y no encima: son el
  // contrapeso de lo que se acaba de leer, y arriba se leerían como un aviso
  // antes de que haya nada de lo que avisar.
  const pie = [];
  if (b.resto) {
    pie.push(
      `<p class="explica">Hay ${cuenta(b.resto, "hallazgo", "hallazgos")} más, ` +
      `que éstos. Están enteros en las vistas de abajo.</p>`,
    );
  }
  if (b.nota_azar) pie.push(`<p class="ficha">${escapar(b.nota_azar)}</p>`);

  return cabeceraBloque(b) + tarjetas + pie.join("");
}

function tarjetaHallazgo(h) {
  const clase = CLASE_VALENCIA[h.valencia] || "neutro";
  const f = h.ficha || {};

  // La fuerza del hallazgo en palabras -«se nota», «apenas»- y no en un número:
  // viene ya traducida del servidor, que es quien sabe contra qué umbral.
  const marca = h.fuerza
    ? `<span class="fuerza ${clase}">${escapar(h.fuerza)}</span>`
    : "";

  const detalle = [];
  if (f.dias_despues !== null && f.dias_despues !== undefined) {
    detalle.push(`${cuenta(f.dias_despues, "día", "días")} después`);
  }
  /* `r` y `p` SE QUEDAN con su nombre de una letra, y es a propósito.
   *
   * Son los dos únicos números de toda la interfaz que no se pueden decir con
   * palabras sin perder algo: «r» es la fuerza CON SIGNO en una escala fija de
   * −1 a 1, y ya está dicha en castellano arriba, en la frase y en la marca de
   * fuerza. Lo que hace aquí abajo es dejar comprobar la frase, y para eso hace
   * falta la cifra, no otra paráfrasis de la cifra.
   *
   * Lo que sí se traduce es todo lo demás de la línea, porque no son números:
   * `n` era una letra puesta en lugar de una frase corta, y `spearman` era una
   * palabra en inglés sin traducir. Ésos no son el dato, son la etiqueta del
   * dato, y una etiqueta que hay que descifrar enseña a saltarse la línea
   * entera -incluidos la `r` y la `p`, que sí importan-. */
  if (f.r !== null && f.r !== undefined) detalle.push(`r = ${num(f.r)}`);
  if (f.p_corregida !== null && f.p_corregida !== undefined) {
    detalle.push(`p = ${num(f.p_corregida, 4)} (corregida)`);
  }
  if (f.n) detalle.push(`${entero(f.n)} pares comparados`);
  if (f.metodo) detalle.push(escapar(METODOS[f.metodo] || f.metodo));

  return (
    `<article class="tarjeta hallazgo">` +
    `<p class="grupo-hallazgo">${escapar(h.grupo.titulo)} · decide ` +
    `${escapar(h.grupo.decision)}</p>` +
    `<p class="frase ${clase}">${escapar(h.frase)}${marca}</p>` +
    (h.matiz ? `<p class="matiz">${escapar(h.matiz)}</p>` : "") +
    (h.nota_confirmacion
      ? `<p class="confirma">${escapar(h.nota_confirmacion)}</p>` : "") +
    (h.nota_discrepancia
      ? `<p class="discrepa">${escapar(h.nota_discrepancia)}</p>` : "") +
    `<p class="ficha">${escapar(detalle.join(" · "))}</p>` +
    `</article>`
  );
}

// ---------------------------------------------------------------------------
// Vista 1: concordancia
// ---------------------------------------------------------------------------

async function pintarConcordancia(dias) {
  const d = await pedir(RUTAS.concordancia, { dias });
  const partes = [
    encabezadoVista(d.encabezado),
    pintarCobertura(d.cobertura, d.ventana),
  ];

  partes.push(
    `<h2 class="grupo">Lo que notas frente al reloj</h2>` +
    `<p class="explica">Cada pareja junta algo que contestas por la mañana con ` +
    `algo que el reloj mide solo. La barra va de −1 a 1 con el cero marcado, y ` +
    `<b>el signo que se espera</b> está escrito debajo: lo interesante no es que ` +
    `la correlación sea alta, es que vaya en la dirección que debería. Estas ` +
    `siete son preguntas hechas de antemano, así que no llevan corrección por ` +
    `comparaciones múltiples: no hay una rejilla que rastrear, hay siete ` +
    `hipótesis. El bloque de abajo sí la lleva, y por eso.</p>`,
  );

  for (const p of d.pares) {
    const dir = p.signo_esperado > 0 ? "suban juntos" : "vaya uno al revés del otro";
    partes.push(
      `<article class="tarjeta">` +
      `<h2>${escapar(p.titulo)}</h2>` +
      `<p class="sub">${escapar(p.etiqueta_x)} · ${escapar(p.etiqueta_y)}</p>` +
      barraR(p) +
      `<p class="cifra">r = <b>${num(p.r)}</b>${p.p !== null && p.p !== undefined
        ? ` · p = ${num(p.p, 3)}` : ""}</p>` +
      (p.lectura
        ? `<p class="lectura">${escapar(p.lectura)}</p>`
        : bloqueNa(p.na)) +
      `<p class="esperado">Se espera que ${dir}.</p>` +
      ficha(p) +
      (p.aviso && p.lectura ? `<p class="na">${escapar(p.aviso)}</p>` : "") +
      `</article>`,
    );
  }

  partes.push(seccionInternas(d));

  // Las señales sueltas, plegadas. Cada una en su propio percentil histórico,
  // que es lo único que permite dibujar un 1-5 y una HRV en milisegundos en la
  // misma escala sin que una de las dos sea una raya pegada al suelo.
  /* Aquí `n` son DÍAS CON DATO, y en la casilla de al lado son pares, y en la
   * de percepción son sesiones. Ésa es la razón de fondo para no dejar la letra
   * suelta en ninguna de las tres: no significa lo mismo, y la única forma de
   * saber cuál toca era saberse de memoria qué vista se estaba mirando.
   *
   * `s.unidad` SÍ se queda con el rango dentro -"0-100"- y es lo correcto: aquí
   * describe la escala de la serie, que es para lo que ese campo sirve. Lo que
   * se arregló en la portada no fue el campo, fue pegarlo detrás de un valor. */
  const series = d.series.map((s) => (
    `<div class="serie">` +
    `<div class="linea"><label>${escapar(s.etiqueta)}</label>` +
    `<output class="valor">${cuenta(s.n, "día", "días")}</output></div>` +
    (s.n
      ? serieTemporal(s.puntos, [0, 100], "escala")
      : bloqueNa(`no hay ni un día con ${escapar(s.etiqueta.toLowerCase())} en esta ventana`)) +
    `<p class="ficha">${escapar(ORIGENES[s.fuente] || s.fuente)} · ` +
    `escala ${escapar(s.unidad)} · ` +
    `${s.sentido === "alto_peor" ? "alto = peor" : "alto = mejor"}` +
    `${s.desplazamiento_dias ? ` · desplazada ${cuenta(s.desplazamiento_dias, "día", "días")}` : ""}` +
    `</p>` +
    // La misma tabla que en la portada y por el mismo motivo, pero aquí importa
    // más: esta es la pantalla donde la serie se CORRELACIONA con otras, y una
    // correlación con un binario que junta dos cosas opuestas hay que leerla
    // sabiendo cuál de las dos la está moviendo.
    tablaDiscordancia(s.tabla) +
    `</div>`
  )).join("");

  partes.push(plegable(
    `Las ${d.series.length} señales por separado`,
    `<p class="explica">Dibujadas por su percentil dentro de la ventana, no por ` +
    `su valor crudo: así se pueden mirar juntas. El número de verdad de cada día ` +
    `viaja en el payload y no se pierde.</p>${series}`,
  ));

  $("vista").innerHTML = partes.join("");
}

/* El reloj cruzado consigo mismo: las diez parejas de las cinco métricas.
 *
 * Van partidas en dos grupos y las independientes primero, aunque el payload las
 * manda en su orden natural. No es capricho de maquetación: es la lectura entera
 * del bloque. Ocho de las diez parejas cruzan un número con otro del que Garmin
 * lo calcula -la nota de sueño con los minutos dormidos, el Body Battery con la
 * variabilidad-, y ahí una correlación alta es la fórmula del reloj asomando, no
 * un hallazgo. Las dos que quedan son las únicas que miden dos cosas de verdad
 * distintas.
 *
 * Todas juntas y en fila, el bloque diría "ocho de diez significativas" y se
 * leería como que el reloj es coherentísimo. Partido, se ve DÓNDE está esa
 * coherencia, que es la diferencia entre una tabla y una respuesta.
 *
 * Ninguna se esconde ni se pliega: las ocho están enteras, con su aviso dentro.
 */
function seccionInternas(d) {
  const r = d.resumen_internas;
  const indep = d.internas.filter((c) => c.mismo_origen === null);
  const compartidas = d.internas.filter((c) => c.mismo_origen !== null);

  const contador =
    `<p class="explica"><b>${entero(r.significativas)} de ${entero(r.calculadas)}</b> ` +
    `parejas calculadas aguantan la corrección. Repartidas: ` +
    `<b>${entero(r.independientes.significativas)} de ` +
    `${entero(r.independientes.parejas)}</b> entre las que miden cosas distintas, ` +
    `<b>${entero(r.comparten_origen.significativas)} de ` +
    `${entero(r.comparten_origen.parejas)}</b> entre las que el reloj calcula una ` +
    `a partir de la otra` +
    (r.en_sentido_contrario
      ? ` · ${entero(r.en_sentido_contrario)} va en sentido contrario al esperado`
      : "") +
    `.</p>`;

  const grupo = (titulo, explica, cartas) => (
    `<h3 class="grupo dentro">${escapar(titulo)}</h3>` +
    `<p class="explica">${escapar(explica)}</p>` +
    (cartas.length
      ? cartas.map(tarjetaInterna).join("")
      : bloqueNa("no hay ni una pareja en este grupo"))
  );

  return (
    `<h2 class="grupo">El reloj consigo mismo</h2>` +
    `<p class="explica">${escapar(d.aviso_internas)}</p>` +
    contador +
    grupo(
      "Las que miden dos cosas distintas",
      "El reloj las obtiene por separado, sin que una entre en el cálculo de la " +
      "otra. Es el único sitio de este bloque donde lo que salga puede ser del " +
      "cuerpo y no de la fórmula.",
      indep,
    ) +
    grupo(
      "Las que el reloj calcula una a partir de la otra",
      "Que salgan altas era de esperar: parte de la relación la pone Garmin al " +
      "construir el número. Lo que informa aquí es una que salga baja o al revés.",
      compartidas,
    )
  );
}

function tarjetaInterna(c) {
  return (
    `<article class="tarjeta">` +
    `<h2>${escapar(c.titulo)}</h2>` +
    barraR(c) +
    `<p class="cifra">r = <b>${num(c.r)}</b>${c.p !== null && c.p !== undefined
      ? ` · p = ${num(c.p, 3)}` : ""}</p>` +
    (c.lectura ? `<p class="lectura">${escapar(c.lectura)}</p>` : bloqueNa(c.na)) +
    // El aviso de origen compartido va DENTRO de la tarjeta y no solo en la
    // cabecera del grupo. Una tarjeta que se lea suelta -buscando, o después de
    // desplazarse- tiene que llevar encima el motivo por el que su número puede
    // no significar lo que parece.
    (c.mismo_origen ? `<p class="na">Ojo: ${escapar(c.mismo_origen)}.</p>` : "") +
    `<p class="esperado">Se espera que ` +
    `${c.signo_esperado > 0 ? "suban juntas" : "vaya una al revés de la otra"}.</p>` +
    ficha(c) +
    `<p class="ficha">` +
    `${c.p_corregida !== null && c.p_corregida !== undefined
      ? `p corregida ${num(c.p_corregida, 3)}` : "sin corregir"}` +
    `${c.significativa === true ? " · aguanta la corrección" : ""}` +
    `${c.significativa === false ? " · no aguanta la corrección" : ""}</p>` +
    (c.aviso && c.lectura ? `<p class="na">${escapar(c.aviso)}</p>` : "") +
    `</article>`
  );
}

// ---------------------------------------------------------------------------
// Vista 2: desfase
// ---------------------------------------------------------------------------

async function pintarDesfase(dias) {
  const d = await pedir(RUTAS.desfase, { dias });
  const partes = [
    encabezadoVista(d.encabezado),
    pintarCobertura(d.cobertura, d.ventana),
  ];

  partes.push(`<p class="explica">${escapar(d.convenio)}</p>`);

  // Primero las parejas con pico, y dentro de esas las de pico distinto de cero:
  // son la respuesta a la pregunta de esta vista. Las que no se pudieron calcular
  // van al final, pero VAN: quitarlas dejaría una pantalla donde todo cuadra.
  const conPico = d.rejilla.filter((f) => f.mejor_desfase !== null && f.mejor_desfase !== undefined);
  const sinPico = d.rejilla.filter((f) => f.mejor_desfase === null || f.mejor_desfase === undefined);
  conPico.sort((a, b) => Math.abs(b.mejor_desfase) - Math.abs(a.mejor_desfase));

  const tarjeta = (f) => (
    `<article class="tarjeta">` +
    `<h2>${escapar(f.etiqueta_x)} · ${escapar(f.etiqueta_y)}</h2>` +
    curvaDesfase(f.por_desfase, f.mejor_desfase) +
    (f.mejor_desfase !== null && f.mejor_desfase !== undefined
      ? `<p class="cifra">Pico en <b>${f.mejor_desfase > 0 ? "+" : ""}` +
        `${cuenta(f.mejor_desfase, "día", "días")}</b></p>`
      : "") +
    (f.lectura ? `<p class="lectura">${escapar(f.lectura)}</p>` : bloqueNa(f.na)) +
    plegable("Los siete desfases, uno a uno", tablaDesfases(f.por_desfase)) +
    `</article>`
  );

  if (conPico.length) {
    partes.push(`<h2 class="grupo">Con pico calculado (${conPico.length})</h2>`);
    partes.push(conPico.map(tarjeta).join(""));
  }
  if (sinPico.length) {
    partes.push(
      `<h2 class="grupo">Sin pico todavía (${sinPico.length})</h2>` +
      `<p class="explica">Siguen aquí a propósito. Una pantalla que solo enseña ` +
      `las parejas que salieron parece decir más de lo que sabe.</p>`,
    );
    partes.push(sinPico.map(tarjeta).join(""));
  }

  $("vista").innerHTML = partes.join("");
}

function tablaDesfases(porDesfase) {
  const filas = Object.keys(porDesfase)
    .map(Number)
    .sort((a, b) => a - b)
    .map((k) => {
      const c = porDesfase[k];
      return (
        `<tr><td>${k > 0 ? "+" : ""}${k}</td>` +
        `<td>${num(c.r)}</td><td>${num(c.p, 3)}</td><td>${entero(c.n)}</td>` +
        `<td class="motivo">${escapar(c.na || "")}</td></tr>`
      );
    })
    .join("");
  /* `r` y `p` se quedan de cabecera y `n` no, que parece incoherente y no lo es.
   * En una tabla de siete filas la cabecera es la ÚNICA explicación que va a
   * tener cada columna, y «r» y «p» al menos identifican sin ambigüedad las dos
   * cifras que hay debajo -están dichas con palabras en la lectura de arriba-.
   * «n» no identificaba nada: el número de esa columna son días, y decir «días»
   * cuesta lo mismo y no hay que traducirlo. */
  return (
    `<table class="tabla"><thead><tr><th>Desfase</th><th>r</th><th>p</th>` +
    `<th>Días</th><th>Motivo si no hay</th></tr></thead><tbody>${filas}</tbody></table>`
  );
}

// ---------------------------------------------------------------------------
// Vista 3: impacto
// ---------------------------------------------------------------------------

async function pintarImpacto(dias) {
  const d = await pedir(RUTAS.impacto, { dias });

  /* EL CATÁLOGO LO MANDA EL SERVIDOR, con cuántas casillas vivas tiene cada
   * respuesta y cuál conviene abrir. Antes se recorría aquí la rejilla y se
   * abría en `respuestas[0]`, que es el cansancio: cero de treinta y tres. La
   * vista se estrenaba vacía con ciento treinta y cinco relaciones calculadas a
   * dos clics de distancia, y no había forma de saberlo sin ir probando las doce
   * opciones una por una.
   *
   * Contarlo aquí además duplicaría el recuento que `catalogo_de_respuestas` ya
   * hace en el servidor, y el día que discreparan ganaría el que no se puede
   * probar. */
  const respuestas = d.respuestas || [];
  const vistas = new Set(respuestas.map((r) => r.clave));
  if (!elegido.respuesta || !vistas.has(elegido.respuesta)) {
    elegido.respuesta = d.respuesta_por_defecto || null;
  }

  const partes = [
    encabezadoVista(d.encabezado),
    pintarCobertura(d.cobertura, d.ventana),
  ];
  partes.push(`<p class="aviso ojo"><strong>Ojo con leer esto como una causa.</strong>` +
    `${escapar(d.advertencia)}</p>`);

  /* Las vacías NO se quitan del desplegable: se marcan y se van al final.
   *
   * Quitarlas escondería que el sistema sabe mirar el dolor lumbar y todavía no
   * tiene con qué, que es información -y de la que hace falta para saber qué
   * vale la pena apuntar-. Marcarlas dice las dos cosas a la vez: existe, y hoy
   * no tiene nada dentro. */
  const conDatos = respuestas.filter((r) => !r.vacia);
  const vacias = respuestas.filter((r) => r.vacia);
  const opcion = (r) => (
    `<option value="${escapar(r.clave)}"` +
    `${r.clave === elegido.respuesta ? " selected" : ""}>` +
    `${escapar(r.etiqueta)}` +
    `${r.vacia ? " — sin datos todavía" : ` (${entero(r.n)})`}` +
    `</option>`
  );

  partes.push(
    `<div class="filtro"><label for="resp">Qué le pasó al cuerpo</label>` +
    `<select id="resp">` +
    (conDatos.length
      ? `<optgroup label="Con datos">${conDatos.map(opcion).join("")}</optgroup>`
      : "") +
    (vacias.length
      ? `<optgroup label="Sin datos todavía">${vacias.map(opcion).join("")}</optgroup>`
      : "") +
    `</select>` +
    `<p class="explica">El número entre paréntesis es cuántas relaciones se han ` +
    `podido calcular con esa respuesta. Las de abajo salen igual aunque no tengan ` +
    `nada: el sistema sabe mirarlas y todavía no hay con qué.</p>` +
    `</div>`,
  );

  // Que NINGUNA respuesta tenga datos es una situación real -el sistema acaba de
  // arrancar-, y se dice. Sin esto se pintaría un desplegable de doce opciones
  // encima de una pantalla vacía, que parece una vista rota.
  if (!elegido.respuesta) {
    partes.push(bloqueNa(
      `ninguna de las ${respuestas.length} respuestas tiene todavía una sola ` +
      `relación calculada. Hace falta que se apunten sesiones -y que pasen los ` +
      `días de después- para que haya algo que cruzar`,
    ));
  }

  const filas = d.rejilla.filter((f) => f.respuesta.clave === elegido.respuesta);
  const familias = {};
  for (const f of filas) (familias[f.exposicion.familia] ||= []).push(f);

  const NOMBRE_FAMILIA = {
    bici: "Salidas de bici",
    rutina: "Rutinas de fuerza",
    fuerza: "Volumen y series",
    ejercicio: "Ejercicios sueltos",
  };

  for (const [familia, lista] of Object.entries(familias)) {
    partes.push(`<h2 class="grupo">${escapar(NOMBRE_FAMILIA[familia] || familia)}</h2>`);
    partes.push(lista.map(tarjetaImpacto).join(""));
  }

  /* EL RANKING SIGUE AL DESPLEGABLE, y antes no lo seguía: estaba clavado a
   * `lower_discomfort`, que es uno de los deslizadores del check-in y hoy no
   * tiene ni un día. O sea que debajo de una vista de impacto llena salía
   * siempre «no hay ni un ejercicio con días suficientes», y parecía que el
   * ranking estaba roto cuando lo que pasaba es que se le estaba preguntando por
   * lo único que nadie ha contestado nunca.
   *
   * Va en serie y no en paralelo con la otra llamada a propósito: cuál es la
   * respuesta que se va a pintar lo decide el servidor en la primera, y pedir el
   * ranking antes de saberlo es lo que obligaba a clavarlo a mano. */
  if (elegido.respuesta) {
    partes.push(seccionRanking(await pedir(RUTAS.ranking, {
      dias, respuesta: elegido.respuesta,
    })));
  }

  $("vista").innerHTML = partes.join("");

  $("resp").addEventListener("change", (ev) => {
    elegido.respuesta = ev.target.value;
    cargar();
  });
}

/* Las dos formas de exposición NO se pintan igual, y no es una preferencia.
 *
 * Una exposición `binaria` -salí en bici o no salí- parte los días en dos grupos
 * y el servidor manda la media de cada uno. Una `continua` -los kilos que moví-
 * no tiene grupos: no hay "los días con esto" porque todos los días tienen un
 * poco de esto.
 *
 * Pintarlas con la misma plantilla dejaba "n = 43 · — con esto · — sin ello"
 * debajo de cada volumen. Esa raya se lee como un dato que falta, y no falta
 * ninguno: es que la pregunta no existe para esa fila. Es el mismo error que
 * rellenar un hueco con un cero, solo que al revés -inventar una ausencia en vez
 * de inventar un valor- y se lee igual de mal.
 */
function tarjetaImpacto(f) {
  const porGrupos = f.exposicion.tipo === "binaria";

  const dias = f.por_dia.map((c) => (
    `<div class="retardo">` +
    `<div class="linea"><label>+${cuenta(c.dias_despues, "día", "días")}</label>` +
    `<output class="valor">${num(c.r)}</output></div>` +
    barraR(c) +
    (porGrupos && c.media_expuesto !== null && c.media_expuesto !== undefined &&
     c.media_no_expuesto !== null && c.media_no_expuesto !== undefined
      ? `<p class="medias"><b>${num(c.media_expuesto)}</b> los días después de ` +
        `esto · <b>${num(c.media_no_expuesto)}</b> los días después de otra cosa` +
        `${c.diferencia !== null && c.diferencia !== undefined
          ? ` · diferencia ${num(c.diferencia)}` : ""}</p>`
      : "") +
    (c.na ? bloqueNa(c.na) : "") +
    `<p class="ficha">${entero(c.n)} días comparados` +
    (porGrupos
      ? ` · ${entero(c.n_expuesto)} con esto · ${entero(c.n_no_expuesto)} sin ello`
      : "") +
    `${c.p_corregida !== null && c.p_corregida !== undefined
      ? ` · p corregida ${num(c.p_corregida, 3)}` : ""}` +
    `${c.significativa === true ? " · aguanta la corrección" : ""}</p>` +
    `</div>`
  )).join("");

  return (
    `<article class="tarjeta">` +
    `<h3>${escapar(f.exposicion.etiqueta)}</h3>` +
    (f.lectura ? `<p class="lectura">${escapar(f.lectura)}</p>` : "") +
    dias +
    `</article>`
  );
}

/* El título de la sección, con `en_frase` y no con la etiqueta en minúsculas.
 *
 * `etiqueta.toLowerCase()` escribía «ordenados por variabilidad (hrv)»: el
 * acrónimo desmontado por el propio navegador. Es un daño pequeño y es de la
 * familia de siempre -lo que se lee no es lo que se guardó, y la diferencia la
 * mete la presentación-, así que se usa el campo que el servidor manda para
 * exactamente esto. */
function tituloRanking(rank) {
  const r = rank.respuesta || {};
  return (
    `<h2 class="grupo">Ejercicios ordenados por ` +
    `${escapar(r.en_frase || r.etiqueta || r.clave)}</h2>`
  );
}

/* LA CORRECCIÓN, QUE ES JUSTO LO QUE ESTA SECCIÓN NO PODÍA CALLARSE.
 *
 * El servidor corrige la tanda ENTERA -`corregir_tanda` en `impacto.py`, sobre
 * los 38 ejercicios por los 3 retardos- y manda `p_corregida` y `significativa`
 * dentro de cada casilla. Esta sección las tiraba a la basura: pintaba con
 * `ficha()`, el resumen compartido de `comun.js`, que no lleva ninguna de las
 * dos. Las tarjetas de impacto de tres centímetros más arriba se montan su
 * propia ficha y sí las escriben, palabra por palabra.
 *
 * O sea que la ÚNICA pantalla del panel que ordena correlaciones en un podio
 * numerado era la única que no decía cuáles aguantan. Y es donde más falta
 * hace, no donde menos: una lista ordenada por r pone en el puesto 1, por
 * construcción, la más extrema de ciento catorce pruebas. Eso no es un hallazgo,
 * es la definición del problema que la corrección existe para arreglar. Medido
 * el 2026-09-17 contra la HRV: de 66 calculadas, NINGUNA aguanta. El podio se
 * veía exactamente igual, con un «r = −0,38» en negrita en el primer puesto.
 *
 * La barra hueca ya lo insinuaba -`barraR` la dibuja sin relleno cuando
 * `significativa === false`- y no basta por tres motivos: es un convenio que
 * esta pantalla no explica en ningún sitio, no llega a un lector de pantalla
 * -su `aria-label` solo dice el número- y compite con una cifra en negrita al
 * lado de un número de puesto.
 */
function resumenCorreccion(rank) {
  const casillas = rank.ranking.flatMap((e) => e.por_dia || []);
  const calculadas = casillas.filter((c) => c.r !== null && c.r !== undefined);
  const aguantan = calculadas.filter((c) => c.significativa === true);
  if (!calculadas.length) return "";
  return (
    `<p class="explica">De las <b>${entero(calculadas.length)}</b> relaciones ` +
    `que se han podido calcular -${cuenta(rank.ranking.length, "ejercicio", "ejercicios")} ` +
    `por ${cuenta(rank.retardos.length, "retardo", "retardos")}-, ` +
    `<b>${entero(aguantan.length)}</b> ${plural(aguantan.length, "aguanta", "aguantan")} ` +
    `la corrección por comparaciones múltiples. ` +
    (aguantan.length
      ? `Las demás están porque esconderlas dejaría una lista que parece decir ` +
        `más de lo que sabe.`
      : `El orden es real -esos son los números que salieron- pero ` +
        `ningún puesto se distingue del azar todavía: una lista ordenada por r ` +
        `pone arriba la más extrema de las que se probaron, y eso pasa también ` +
        `cuando no hay nada debajo. Sirve para elegir por dónde mirar, no para ` +
        `concluir.`) +
    `</p>`
  );
}

/* El veredicto de UNA casilla, para la ficha de cada puesto del ranking.
 *
 * Aquí se escribe también el «no», y en `tarjetaImpacto` no. No es un descuido
 * copiando: es que el texto que hace falta depende de con qué compita. En una
 * tarjeta de impacto el número va solo, sin puesto y sin vecinos, y el silencio
 * se lee como lo que es. En un podio numerado el número compite con un «1» al
 * lado, y el «1» ya está afirmando algo por su cuenta; callarse ahí deja que lo
 * afirme sin contestación. La frase corta que lo contesta cabe en la misma
 * línea.
 *
 * Se devuelve `""` cuando no hay casilla o no hay `r`: sin correlación no hay
 * nada que corregir, y el motivo ya lo ha escrito `bloqueNa` dos líneas arriba.
 * Y si el servidor manda `r` sin veredicto -hoy no pasa: `corregir_tanda` marca
 * las 114 casillas- se escribe la p sola y no se inventa un fallo. */
function veredicto(c) {
  if (!c || c.r === null || c.r === undefined) return "";
  const p = c.p_corregida !== null && c.p_corregida !== undefined
    ? ` · p corregida ${num(c.p_corregida, 3)}`
    : "";
  if (c.significativa === true) return `${p} · aguanta la corrección`;
  if (c.significativa === false) return `${p} · no aguanta la corrección`;
  return p;
}

function seccionRanking(rank) {
  if (!rank.ranking.length) {
    return (
      tituloRanking(rank) +
      bloqueNa(
        `no hay ni un ejercicio con días suficientes en esta ventana ` +
        `(${entero(rank.n_ejercicios)} encontrados). El ranking existe y está ` +
        `vacío: no es que se esconda, es que aún no hay con qué llenarlo`,
      )
    );
  }

  /* El retardo por el que el SERVIDOR ha ordenado la lista, que es el primero de
   * `retardos`. Se busca por `dias_despues` y no se coge `por_dia[0]` a ciegas:
   * si algún día el orden de esa lista cambiara, coger el primero pintaría un
   * número al lado de la frase "ordenados por correlación a +1 día" que sería de
   * otro retardo, y no habría forma de notarlo mirando la pantalla. */
  const principal = (e) => {
    const k = rank.retardos[0];
    return e.por_dia.find((c) => c.dias_despues === k) || null;
  };

  const filas = rank.ranking.map((e, i) => {
    const c = principal(e);
    return (
      `<article class="tarjeta fina">` +
      `<h3><span class="puesto">${i + 1}</span>${escapar(e.etiqueta || e.clave)}</h3>` +
      (c === null
        // No es lo mismo que una correlación incalculable: es que el ejercicio
        // no trae la casilla del retardo por el que se ordenó todo.
        ? bloqueNa(
          `este ejercicio no trae la casilla de +${entero(rank.retardos[0])} ` +
          `${plural(rank.retardos[0], "día", "días")}, que es justo por la que está `
          + `ordenada esta lista`,
        )
        : barraR(c) +
          (c.r !== null && c.r !== undefined
            ? `<p class="cifra">r = <b>${num(c.r)}</b> a ${escapar(rank.ordenado_por)}</p>`
            : bloqueNa(c.na)) +
          ficha(c)) +
      `<p class="ficha">hecho ${cuenta(e.veces_hecho, "día", "días")} en esta ventana` +
      veredicto(c) + `</p>` +
      `</article>`
    );
  }).join("");

  return (
    tituloRanking(rank) +
    `<p class="explica">Ordenados por ${escapar(rank.ordenado_por)}. Los que no ` +
    `tienen con qué compararse salen igual, los últimos y con el motivo escrito: ` +
    `un ejercicio que se hace TODOS los días no tiene días sin él, y esconderlo ` +
    `por eso sería esconder justo el que más se hace.</p>` +
    resumenCorreccion(rank) +
    filas
  );
}

// ---------------------------------------------------------------------------
// Vista 4: auditoría
// ---------------------------------------------------------------------------

async function pintarAuditoria(dias) {
  const d = await pedir(RUTAS.auditoria, { dias });
  const g = d.distribucion.global;
  // Los nombres los manda el servidor. Esto NO es un detalle de estilo: aquí
  // había tres colores escritos a mano en tres sitios distintos de la PWA, y el
  // cuarto -el reparto en porcentaje- ni siquiera los tenía escritos, así que
  // pintaba la clave cruda. Con la tabla del servidor, un color nuevo aparece
  // en los cuatro sitios sin tocar nada aquí.
  const nombre = (luz) => (d.nombres_luz || {})[luz] || luz;
  const partes = [
    encabezadoVista(d.encabezado),
    pintarCobertura(d.cobertura, d.ventana),
  ];

  partes.push(
    `<article class="tarjeta">` +
    `<h2>El reparto de luces</h2>` +
    `<div class="luces">` +
    `<span class="luz green">${entero(g.green)} ${escapar(nombre("green"))}</span>` +
    `<span class="luz amber">${entero(g.amber)} ${escapar(nombre("amber"))}</span>` +
    `<span class="luz red">${entero(g.red)} ${escapar(nombre("red"))}</span>` +
    `<span class="luz nada">${entero(g.sin_decision)} sin decisión</span>` +
    `</div>` +
    // El porcentaje va DEBAJO de las fichas y con su denominador pegado, no
    // suelto y en grande. Las fichas de arriba dicen "0 verde · 1 ámbar · 0
    // rojo · 179 sin decisión"; este mismo reparto en tanto por ciento es
    // "100,0 % ámbar", que leído solo significa "todos los días han sido
    // ámbar" y es falso: son todos los días CON DECISIÓN, que aquí es uno.
    // Los días sin decisión no entran en el denominador a propósito -lo
    // explica `distribucion()` en `auditoria.py`- y por eso hay que decir
    // cuántos se quedaron fuera; si no, el 100 % se lee sobre la ventana
    // entera. Un porcentaje sin su denominador al lado no es un dato
    // incompleto: es un dato distinto.
    (g.porcentaje
      ? `<p class="ficha">${escapar(
          `Sobre ${cuenta(g.n, "día", "días")} con decisión` +
          (g.sin_decision
            ? ` (${cuenta(g.sin_decision, "día", "días")} sin ella fuera de la cuenta)`
            : "") +
          ": " +
          Object.entries(g.porcentaje)
            .map(([k, v]) => `${pct(v, 1)} ${nombre(k)}`)
            .join(" · "),
        )}</p>`
      : bloqueNa(
          "no hay ni un día con decisión guardada en esta ventana, así que no " +
          "hay reparto que enseñar: no es un cero, es que no hay denominador",
        )) +
    `<div class="desliza">${calendario(d.dias)}</div>` +
    `<p class="ficha">Un cuadro por día. Los días sin decisión son los huecos ` +
    `con borde: están a la vista porque son parte de lo que hay que auditar.</p>` +
    `<div class="desliza">${barrasSemanales(d.distribucion.por_semana, d.nombres_luz)}</div>` +
    `<p class="ficha">Una barra por semana, sobre los siete días enteros.</p>` +
    `</article>`,
  );

  // Las reglas. Las que nunca dispararon van primero y no al final: son las que
  // hay que mirar, y la distinción entre "se evaluó y no saltó" y "no se pudo
  // evaluar ni una vez" es la que salva esta vista entera.
  // `no_hizo_falta` va con `dispara` y no con las de arriba: es el ámbar por
  // precaución en una ventana en la que el reloj siempre llegó a tiempo, o sea
  // nada que mirar. Puesto entre las que "o están mal calibradas o sobran"
  // mandaría a arreglar lo único que no falló.
  const orden = {
    nunca_evaluada: 0, nunca_disparo: 1, retirada: 2,
    dispara: 3, no_hizo_falta: 4, sin_historico: 5,
  };
  const reglas = [...d.reglas].sort(
    (a, b) => (orden[a.estado] ?? 9) - (orden[b.estado] ?? 9),
  );

  partes.push(`<h2 class="grupo">Las ${reglas.length} reglas</h2>`);
  partes.push(reglas.map((r) => (
    `<article class="tarjeta fina estado-${escapar(r.estado)}">` +
    // Sin luz, sin insignia. Una regla retirada del `config.yaml` -`resaca_finde`
    // ahora mismo- no tiene color declarado, y esto pintaba la pastilla igual:
    // un óvalo con borde y nada dentro, al lado del nombre. No es un fallo que
    // se pueda ver en el payload, porque ahí `luz` vale `null` y eso es
    // correcto; solo se ve en la pantalla, y parece un texto que no cargó.
    `<h3>${escapar(r.nombre)}` +
    (r.luz
      ? ` <span class="etiqueta-luz ${escapar(r.luz)}">` +
        `${escapar(r.nombre_luz || r.luz)}</span>`
      : "") +
    `</h3>` +
    (r.descripcion ? `<p class="sub">${escapar(r.descripcion)}</p>` : "") +
    `<p class="lectura">${escapar(r.lectura)}</p>` +
    `<p class="ficha">disparó ${entero(r.veces_disparada)} · mandó ` +
    `${entero(r.veces_determinante)} · evaluada ${cuenta(r.dias_evaluada, "día", "días")} · ` +
    `saltada ${entero(r.veces_saltada)}` +
    `${r.ultima_vez ? ` · última vez ${fechaCorta(r.ultima_vez)}` : ""}</p>` +
    (Object.keys(r.le_falto || {}).length
      ? `<p class="ficha">le faltó: ${escapar(
          Object.entries(r.le_falto).map(([k, v]) => `${k} (${cuenta(v, "día", "días")})`).join(", "),
        )}</p>`
      : "") +
    `</article>`
  )).join(""));

  partes.push(seccionLista(
    "Reglas especiales", d.reglas_especiales,
    (e) => (
      `<h3>${escapar(e.nombre)}${e.declarada ? "" : " <span class=\"retirada\">retirada</span>"}</h3>` +
      `<p class="lectura">${escapar(e.lectura)}</p>` +
      // `cuenta`, no "vez/veces". Es el mismo plural de plantilla que se sacó
      // de las diecisiete frases del paquete `app/analysis` -y que allí vigila
      // un test para todo el paquete-, sobreviviendo aquí porque aquel test
      // mira Python y esta frase está en JavaScript. Llevaba escrito "activada
      // 0 vez/veces" en la pantalla del móvil todo este tiempo.
      `<p class="ficha">activada ${cuenta(e.veces_activada, "vez", "veces")}</p>` +
      // Cada activación con su tramo y su motivo. Sin esto, "se activó 3 veces"
      // no dice si fueron tres días o seis semanas, ni sobre qué ejercicio.
      (e.activaciones.length
        ? `<ul class="motivos">` + e.activaciones.map((a) => (
          `<li>${fechaCorta(a.desde)} → ` +
          `${a.hasta ? fechaCorta(a.hasta) : "<b>sigue activa</b>"}` +
          `${a.entidad ? ` · ${escapar(a.entidad)}` : ""}` +
          `${a.motivo ? ` — ${escapar(a.motivo)}` : ""}</li>`
        )).join("") + `</ul>`
        : "")
    ),
    "no hay ninguna regla especial declarada en el config.yaml",
  ));

  /* Una recalibración NO trae frase escrita: trae el hash de antes y el de
   * después, y ya está. La frase se pone aquí porque es siempre la misma y no
   * depende de ningún dato -"ese día cambió el config.yaml"-, que es lo único
   * que el hash permite afirmar. Los dos hashes van enteros y a la vista: son la
   * costura por la que hay que leer con cuidado los contadores de las reglas, y
   * una costura que no se puede señalar con el dedo no sirve de nada. */
  partes.push(seccionLista(
    "Recalibraciones", d.recalibraciones,
    (x) => (
      `<h3>${fechaCorta(x.fecha)}</h3>` +
      `<p class="lectura">Ese día cambió el <code>config.yaml</code>: los ` +
      `contadores de las reglas de antes y de después de esta fecha no hablan ` +
      `de la misma configuración.</p>` +
      `<p class="ficha">${escapar(x.de)} → ${escapar(x.a)}</p>`
    ),
    d.lecturas.recalibraciones,
  ));

  /* Las tres puertas del motor van por separado porque lo son: frenar la subida
   * de carga, no dejar añadir una serie y no dejar sumar repeticiones son tres
   * decisiones distintas con tres motivos distintos. Juntarlas en una línea
   * -"ese día no progresó"- es exactamente lo que esta sección existe para no
   * hacer. Una puerta sin motivo apuntado se dice, no se deja en blanco. */
  const puerta = (abierta, motivo, nombre) => {
    if (abierta) return "";
    return (
      `<p class="lectura"><b>${escapar(nombre)}</b>: ` +
      `${motivo ? escapar(motivo) : ""}</p>` +
      (motivo ? "" : bloqueNa(
        `cerrada sin motivo apuntado. El motor guarda siempre el porqué, así ` +
        `que esto es un hueco del registro y no una explicación`,
      ))
    );
  };

  partes.push(seccionLista(
    "Puertas cerradas", d.puertas_cerradas,
    (x) => (
      // `x.luz` es la clave del motor -"amber"-, no el nombre. Esta insignia
      // estaba pintando la clave tal cual, y se pasó por alto en la misma
      // pasada que arregló el reparto de porcentajes: la insignia de cada
      // REGLA sí usaba `nombre_luz`, así que a ojo parecía que todas lo hacían.
      `<h3>${fechaCorta(x.fecha)} ` +
      `<span class="etiqueta-luz ${escapar(x.luz || "")}">` +
      `${escapar(x.luz ? nombre(x.luz) : "sin luz")}</span></h3>` +
      (x.rutina ? `<p class="sub">${escapar(x.rutina)}</p>` : "") +
      puerta(x.puerta_abierta, x.motivo, "No subió la carga") +
      puerta(x.series_permitidas, x.motivo_series, "No se añadió serie") +
      puerta(x.reps_permitidas, x.motivo_reps, "No se sumaron repeticiones")
    ),
    d.lecturas.puertas_cerradas,
  ));

  partes.push(seccionLista(
    "Progresión de cada ficha", d.progresion,
    (p) => (
      `<h3>${escapar(p.ejercicio)} <span class="sub">${escapar(p.rutina)}</span></h3>` +
      `<p class="lectura">${escapar(p.lectura)}</p>` +
      `<p class="ficha">prescrito ${cuenta(p.dias_prescrito, "día", "días")} · ` +
      `${cuenta(p.subidas.length, "subida", "subidas")} · `
      + `${cuenta(p.frenos.length, "freno", "frenos")}</p>`
    ),
    d.lecturas.progresion,
  ));

  $("vista").innerHTML = partes.join("");
}

/* Una sección que puede estar vacía, y que si lo está DICE por qué.
 *
 * El motivo se pasa de fuera porque el backend ya lo escribió: "no hay ni un día
 * con decisión guardada" y "hubo días y no pasó nunca" son dos vacíos distintos
 * y solo el servidor sabe cuál de los dos es. */
function seccionLista(titulo, lista, fila, motivoVacio) {
  if (!lista || !lista.length) {
    return `<h2 class="grupo">${escapar(titulo)}</h2>${bloqueNa(motivoVacio)}`;
  }
  return (
    `<h2 class="grupo">${escapar(titulo)} (${lista.length})</h2>` +
    lista.map((x) => `<article class="tarjeta fina">${fila(x)}</article>`).join("")
  );
}

// ---------------------------------------------------------------------------
// Vista 5: percepción frente a rendimiento
// ---------------------------------------------------------------------------

async function pintarPercepcion(dias) {
  const d = await pedir(RUTAS.percepcion, { dias });
  const partes = [encabezadoVista(d.encabezado)];

  for (const [k, v] of Object.entries(d.componentes || {})) {
    if (v.corta) nombresCortos[k] = v.corta;
  }

  // EL CONTADOR VA PRIMERO Y VA GRANDE. Es el dato que se pidió mirar la
  // próxima mañana de levantarse pensando que no se puede entrenar, así que no
  // puede estar debajo de una tabla ni detrás de un desplegable. Y va el
  // ACUMULADO, no el de la ventana: el de la ventana baja al mover el selector,
  // y un contador que se encoge al cambiar de pantalla no sirve para apoyarse.
  const h = d.historico;
  partes.push(
    `<section class="contador">` +
    (h.veces !== null && h.veces !== undefined && h.de
      ? `<p class="enorme">${entero(h.veces)}</p>` +
        `<p class="pie">veces de ${entero(h.de)} que te veías peor de lo que ` +
        `luego estuviste${h.pct !== null && h.pct !== undefined
          ? ` · ${pct(h.pct)}` : ""}</p>`
      : bloqueNa(h.na)) +
    `<p class="ficha">${escapar(h.que_es)}</p>` +
    `</section>`,
  );

  if (d.mensaje) {
    partes.push(`<article class="tarjeta mensaje"><h2>Lo último que se te mandó</h2>` +
      `<pre>${escapar(d.mensaje)}</pre></article>`);
  }

  if (d.ultima) partes.push(tarjetaSesion(d.ultima, "La última sesión juzgada"));

  partes.push(pintarCobertura(null, d.ventana));

  const c = d.contador;
  partes.push(
    `<article class="tarjeta">` +
    `<h2>En esta ventana</h2>` +
    (c.de
      ? `<p class="cifra"><b>${entero(c.veces)}</b> de ${entero(c.de)} sesiones ` +
        `juzgadas${c.pct !== null && c.pct !== undefined ? ` · ${pct(c.pct)}` : ""}</p>`
      : bloqueNa(c.na)) +
    `<p class="ficha">${entero(c.total_sesiones)} sesiones registradas · ` +
    `${entero(c.sin_juicio)} sin poder juzgar · ${entero(d.alineadas)} alineadas</p>` +
    `<p class="ficha">${escapar(c.nota)}</p>` +
    (Object.keys(c.motivos || {}).length
      ? plegable("Por qué no se pudieron juzgar", `<ul class="motivos">` +
        Object.entries(c.motivos).map(([m, n]) => (
          `<li><b>${entero(n)}</b> — ${escapar(m)}</li>`
        )).join("") + `</ul>`)
      : "") +
    `</article>`,
  );

  // La dirección contraria, contada y SIN destacar. Va en su sitio, con su
  // número, porque un marcador que solo apunta los aciertos es un cartel.
  const k = d.contraria;
  partes.push(
    `<article class="tarjeta fina">` +
    `<h3>La otra dirección</h3>` +
    (k.de
      ? `<p class="cifra">${entero(k.veces)} de ${entero(k.de)}` +
        `${k.pct !== null && k.pct !== undefined ? ` · ${pct(k.pct)}` : ""}</p>`
      : bloqueNa(k.na)) +
    `<p class="ficha">${escapar(k.que_es)}</p>` +
    `</article>`,
  );

  partes.push(
    `<article class="tarjeta fina"><h3>Por tipo de sesión</h3>` +
    `<table class="tabla"><thead><tr><th></th><th>Sesiones</th><th>Juzgadas</th>` +
    `<th>Veces</th></tr></thead><tbody>` +
    Object.entries(d.por_tipo).map(([tipo, v]) => (
      `<tr><td>${tipo === "strength" ? "Fuerza" : "Bici"}</td>` +
      `<td>${entero(v.sesiones)}</td><td>${entero(v.juzgadas)}</td>` +
      `<td>${entero(v.veces)}</td></tr>`
    )).join("") +
    `</tbody></table></article>`,
  );

  partes.push(
    `<article class="tarjeta fina"><h3>Las piezas del índice, por separado</h3>` +
    `<p class="explica">Un 70 de media no dice de dónde sale. Esto sí: enseña si ` +
    `lo sostiene el cumplimiento mientras la progresión lleva meses plana.</p>` +
    // La etiqueta la manda el servidor. Si algún día faltara se ve la clave, que
    // es fea pero cierta; lo que no se hace es tener aquí una segunda lista de
    // nombres que se quede vieja sin que nada lo diga.
    Object.entries(d.componentes).map(([nombre, v]) => (
      `<div class="componente">` +
      `<div class="linea"><label>${escapar(v.etiqueta || nombre)}` +
      `${v.entra_en_el_indice ? "" : ' <span class="sub">(no entra en el índice)</span>'}` +
      `</label><output class="valor">${num(v.media, 1)}</output></div>` +
      (v.media === null || v.media === undefined ? bloqueNa(v.na) : "") +
      // Y aquí `n` son SESIONES. Tercer significado de la misma letra en la
      // misma pantalla; por eso ninguna de las tres la lleva ya.
      `<p class="ficha">media de ${cuenta(v.n, "sesión", "sesiones")}</p></div>`
    )).join("") +
    `</article>`,
  );

  partes.push(listaSesiones(
    "Los días en que pasó", d.disociaciones,
    "todavía no ha pasado ninguna vez en esta ventana, o no se ha podido juzgar " +
    "ninguna sesión. Cuando pase, cada día sale aquí con sus números",
  ));

  partes.push(listaSesiones(
    "Los días en que fue al revés", d.contrarias,
    "ninguna sesión de esta ventana salió por debajo de lo que la mañana prometía",
  ));

  partes.push(plegable(
    `Todas las sesiones de la ventana (${d.sesiones.length})`,
    d.sesiones.length
      ? d.sesiones.map((s) => tarjetaSesion(s)).join("")
      : bloqueNa("no hay ninguna sesión registrada en esta ventana"),
  ));

  $("vista").innerHTML = partes.join("");
}

function listaSesiones(titulo, lista, motivoVacio) {
  if (!lista || !lista.length) {
    return `<h2 class="grupo">${escapar(titulo)}</h2>${bloqueNa(motivoVacio)}`;
  }
  return (
    `<h2 class="grupo">${escapar(titulo)} (${lista.length})</h2>` +
    lista.map((s) => tarjetaSesion(s)).join("")
  );
}

function tarjetaSesion(s, titulo) {
  const piezas = Object.entries(s.componentes || {})
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${escapar(nombresCortos[k] || k)} ${num(v, 1)}`)
    .join(" · ");

  return (
    `<article class="tarjeta fina${s.disociacion ? " destacada" : ""}">` +
    `<h3>${escapar(titulo || fechaCorta(s.fecha))}` +
    `<span class="sub"> ${s.tipo === "strength" ? "fuerza" : "bici"}` +
    `${s.rutina ? ` · ${escapar(s.rutina)}` : ""}</span></h3>` +
    (titulo ? `<p class="sub">${fechaCorta(s.fecha)}</p>` : "") +
    reglaPercentiles(s.percepcion_pct, s.rendimiento_pct) +
    (s.gap !== null && s.gap !== undefined
      ? `<p class="cifra">Hiciste <b>${num(s.gap, 0)}</b> puntos de percentil ` +
        `por ${Number(s.gap) >= 0 ? "encima" : "debajo"} de lo que esperabas</p>`
      : bloqueNa(s.na)) +
    `<p class="ficha">esperabas ${num(s.percepcion, 1)} (percentil ` +
    `${num(s.percepcion_pct, 0)}) · hiciste ${num(s.rendimiento, 1)} (percentil ` +
    `${num(s.rendimiento_pct, 0)}) · ${entero(s.n_base)} sesiones detrás</p>` +
    (piezas ? `<p class="ficha">${piezas}</p>` : "") +
    `</article>`
  );
}

// ---------------------------------------------------------------------------
// Vista 6: el umbral de la bici
// ---------------------------------------------------------------------------

/* DOS PREGUNTAS, Y VAN EN ESTE ORDEN.
 *
 * Primero a partir de cuánta bici se nota, y después cuánto dura lo que se nota.
 * El orden no es de maquetación: la segunda pregunta se calcula SOBRE la
 * respuesta de la primera -la curva de arriba es la de las salidas por encima
 * del corte-, así que leerlas al revés deja un «cuesta un día» flotando sin
 * saber un día de qué.
 *
 * Y cada una va con su control al lado, arriba y sin plegar. El de la primera es
 * la relación suave entre carga y HRV: si esa aguanta la corrección y el corte
 * no, lo que hay es una cuesta y el umbral es un punto cualquiera de ella. El de
 * la segunda es cuántas salidas aisladas hay detrás, que casi siempre son pocas.
 * Los dos dicen «esto podría no ser nada», y por eso están donde se leen y no
 * detrás de un triángulo que hay que tocar.
 */
async function pintarUmbral(dias) {
  const d = await pedir(RUTAS.umbral, { dias });

  const partes = [
    encabezadoVista(d.encabezado),
    pintarCobertura(d.cobertura, d.ventana),
    `<p class="ficha">${escapar(d.salidas.resumen)}</p>`,
    seccionFrontera(d),
    seccionTramos(d.umbral),
    plegableCandidatos(d.umbral.frontera),
    seccionRecuperacion(d.recuperacion, d.sin_p),
    // El convenio es la letra pequeña de TODA la pantalla -contra qué se compara
    // cada número- y por eso va al final y una sola vez, en vez de repetido en
    // cada tarjeta o escondido donde no se lea.
    `<p class="aviso tenue">${escapar(d.convenio)}</p>`,
  ];

  $("vista").innerHTML = partes.join("");
}

/* La ficha de un resumen del servidor: cuántas salidas hay detrás y entre qué
 * valores está la mitad central.
 *
 * El recorrido va SIEMPRE que haya media, y no es adorno. Una media de −9 ms con
 * las salidas repartidas entre −10 y −8, y una media de −9 ms con las salidas
 * repartidas entre −40 y +20, son el mismo número contando dos cosas que no se
 * parecen en nada. Sin el recorrido, la segunda se lee como la primera.
 *
 * `na` y `aviso` no salen nunca juntos porque el servidor no los manda juntos: o
 * no hay número, o lo hay y puede llevar advertencia. */
function fichaResumen(r) {
  if (r.media === null || r.media === undefined) return bloqueNa(r.na);
  return (
    `<p class="ficha">${cuenta(r.n, "salida", "salidas")} · la mitad central ` +
    `entre ${num(r.p25)} y ${num(r.p75)} ms</p>` +
    (r.aviso ? `<p class="aviso">${escapar(r.aviso)}</p>` : "")
  );
}

function seccionFrontera(d) {
  const f = d.umbral.frontera;
  const g = d.grafica;

  // El dibujo se pinta HAYA O NO HAYA corte. Sin corte no lleva la raya naranja
  // ni los palos de dos colores, y sigue contando lo mismo que contaría un
  // cuaderno: cuándo saliste y qué hizo la HRV. Esconderlo porque el análisis no
  // ha encontrado nada dejaría la pantalla sin lo único que no depende de que el
  // análisis encuentre algo.
  const dibujo =
    `<article class="tarjeta">` +
    (g.na ? bloqueNa(g.na) : hrvConSalidas(g) + pieGrafica(g)) +
    `</article>`;

  const titulo = `<h2 class="grupo">A partir de cuánta bici se nota</h2>`;

  if (f.na) {
    return (
      titulo +
      `<article class="tarjeta">${bloqueNa(f.na)}</article>` +
      dibujo +
      bloqueContinua(f)
    );
  }

  const c = f.corte;
  return (
    titulo +
    // Grande, como el contador de percepción, porque es el número que se venía a
    // buscar. Y redondeado a entero igual que lo redondea la frase del servidor:
    // pintar aquí «150,8» debajo de una lectura que dice «de 151 para arriba»
    // sería enseñar dos umbrales distintos en dos renglones seguidos. El valor
    // sin redondear está en la ficha, que es donde se mira cuando importa.
    `<section class="contador">` +
    `<p class="enorme">${num(f.carga, 0)}</p>` +
    `<p class="pie">de carga de entreno en una salida</p>` +
    `<p class="ficha">el corte cae exactamente en ${num(f.carga, 1)} · ` +
    `${cuenta(c.n_debajo, "salida", "salidas")} por debajo · ` +
    `${entero(c.n_encima)} por encima</p>` +
    `</section>` +
    `<article class="tarjeta">` +
    `<p class="lectura">${escapar(f.lectura)}</p>` +
    `<div class="retardo">` +
    `<div class="linea"><label>Por debajo de ${num(f.carga, 0)}</label>` +
    `<output class="valor">${num(c.debajo.media)}</output></div>` +
    fichaResumen(c.debajo) +
    `</div>` +
    `<div class="retardo">` +
    `<div class="linea"><label>De ${num(f.carga, 0)} para arriba</label>` +
    `<output class="valor">${num(c.encima.media)}</output></div>` +
    fichaResumen(c.encima) +
    `</div>` +
    (c.diferencia !== null && c.diferencia !== undefined
      ? `<p class="medias">Diferencia: <b>${num(c.diferencia)}</b> ms` +
        `${c.p_corregida !== null && c.p_corregida !== undefined
          ? ` · p corregida ${num(c.p_corregida, 3)}` : ""}` +
        `${c.significativa === true ? " · aguanta la corrección" : ""}` +
        `${c.significativa === false ? " · no aguanta la corrección" : ""}</p>`
      : "") +
    ficha(c) +
    `</article>` +
    dibujo +
    bloqueContinua(f)
  );
}

/* El pie del dibujo: qué es cada cosa de las que se ven.
 *
 * Una gráfica sin leyenda con tres trazos distintos obliga a adivinar cuál es
 * cuál, y adivinar mal aquí es leer la media móvil como si fueran las medidas.
 * Los números del pie -el suavizado, el máximo, el rango- los manda el servidor;
 * aquí solo se nombran. */
function pieGrafica(g) {
  const trozos = [];
  if (g.suavizado) {
    trozos.push(`la línea es la media de ${cuenta(g.suavizado, "día", "días")}`);
  }
  if (g.rango) trozos.push(`de ${num(g.rango[0], 1)} a ${num(g.rango[1], 1)} ms`);
  if (g.banda) {
    trozos.push(`la franja es tu mitad central, ${num(g.banda.desde, 1)}–` +
      `${num(g.banda.hasta, 1)}`);
  }
  if (g.carga_maxima !== null && g.carga_maxima !== undefined) {
    trozos.push(`el palo más alto es ${num(g.carga_maxima, 0)} de carga`);
  }
  trozos.push(`${cuenta(g.n, "noche medida", "noches medidas")}`);
  return (
    `<p class="ficha">${escapar(trozos.join(" · "))}</p>` +
    (g.corte !== null && g.corte !== undefined
      ? `<p class="explica">La raya de puntos es el corte. Los palos que la ` +
        `pasan van en naranja; los que no, apagados.</p>`
      : "")
  );
}

/* EL CONTROL, Y VA ARRIBA AUNQUE ESTROPEE EL HALLAZGO.
 *
 * Un corte siempre se puede encontrar: parte los días por donde sea y una de las
 * dos mitades saldrá más baja que la otra. Lo que distingue un umbral de una
 * raya arbitraria es que la relación SUAVE -cuanta más carga, peor- no explique
 * ya lo mismo. Por eso la correlación continua va aquí, con su barra, y no
 * dentro del plegable de los candidatos: es lo que dice si el número grande de
 * arriba significa algo.
 *
 * La frase la escribe el servidor en `escalon`, que es quien tiene las dos
 * correcciones de la misma tanda delante. Aquí se compararían dos booleanos y
 * saldría la misma frase hoy, y el día que cambiara la regla de corrección esta
 * pantalla seguiría diciendo lo de antes. */
function bloqueContinua(f) {
  const k = f.continua;
  if (!k) return "";
  return (
    `<article class="tarjeta">` +
    `<h3>¿Escalón o cuesta?</h3>` +
    `<p class="explica">Lo mismo sin partir por ningún sitio: la carga de cada ` +
    `salida contra la HRV del día siguiente, tal cual.</p>` +
    barraR(k) +
    (k.r !== null && k.r !== undefined
      ? `<p class="cifra">r = <b>${num(k.r)}</b></p>`
      : bloqueNa(k.na)) +
    ficha(k) +
    (k.p_corregida !== null && k.p_corregida !== undefined
      ? `<p class="ficha">p corregida ${num(k.p_corregida, 3)}` +
        `${k.significativa === true ? " · aguanta la corrección" : ""}` +
        `${k.significativa === false ? " · no aguanta la corrección" : ""}</p>`
      : "") +
    (f.escalon ? `<p class="lectura">${escapar(f.escalon)}</p>` : "") +
    `</article>`
  );
}

/* LA TABLA DE TRAMOS SE PARTE POR CUARTILES Y EL CORTE SE BUSCA POR DECILES, y
 * eso hay que decirlo donde se ve.
 *
 * Son dos reglas distintas a propósito -lo explica la cabecera de `umbral.py`- y
 * no tienen por qué coincidir: la tabla es para ver la forma de la cosa de punta
 * a punta, y el corte es para encontrar el escalón. Pero sin avisar, la tabla
 * puede decir «de 92 a 174 la HRV sube» justo encima de una frase que dice «de
 * 151 para arriba baja 7 ms», y las dos serían verdad y parecerían reñidas.
 *
 * La marca la calcula el SERVIDOR (`parte_la_frontera`). Compararla aquí -«¿cae
 * el corte entre estos dos bordes?»- sería la misma cuenta hecha dos veces en
 * dos sitios, y el día que una de las dos cambiara de criterio sobre los bordes
 * la marca se pondría en la fila de al lado sin que nada fallara. */
function seccionTramos(u) {
  const titulo = `<h2 class="grupo">La forma entera, tramo a tramo</h2>`;
  if (u.na) return titulo + bloqueNa(u.na);

  const parte = u.tramos.some((t) => t.parte_la_frontera);

  return (
    titulo +
    `<article class="tarjeta">` +
    `<p class="explica">Tus salidas repartidas en cuatro grupos del mismo tamaño ` +
    `por carga, y lo que hizo la HRV la mañana de después de cada uno. Esta tabla ` +
    `se parte en cuatro y el corte de arriba se busca en diez: son dos reglas ` +
    `distintas y no tienen por qué caer en el mismo sitio.</p>` +
    (parte
      ? `<p class="explica">Por eso hay una fila señalada, y por eso su media ` +
        `puede ir en contra de lo de arriba: ese tramo tiene dentro salidas de ` +
        `los dos lados del corte, y su media las promedia juntas. No se contradice ` +
        `con el hallazgo, es que está contando otra cosa.</p>`
      : "") +
    `<table class="tabla"><thead><tr><th>Tramo</th><th>Salidas</th>` +
    `<th>HRV al día siguiente</th><th>Mitad central</th></tr></thead><tbody>` +
    u.tramos.map((t) => (
      `<tr${t.parte_la_frontera ? ' class="parte"' : ""}>` +
      `<td>${escapar(t.etiqueta)}${t.parte_la_frontera
        ? ` <span class="sub">— parte a caballo del corte</span>` : ""}</td>` +
      `<td>${entero(t.n)}</td>` +
      (t.media === null || t.media === undefined
        ? `<td class="motivo" colspan="2">${escapar(t.na || "")}</td>`
        : `<td>${num(t.media)} ms${t.aviso ? " *" : ""}</td>` +
          `<td>${num(t.p25)} a ${num(t.p75)}</td>`) +
      `</tr>`
    )).join("") +
    `</tbody></table>` +
    (u.tramos.some((t) => t.aviso)
      ? `<p class="ficha">* muy pocas salidas en ese tramo para fiarse de la ` +
        `media. El número está, y está marcado.</p>`
      : "") +
    (parte
      ? ""
      : `<p class="ficha">El corte de arriba cae justo en un borde de la tabla, ` +
        `o fuera de ella: ningún tramo se lo lleva dentro.</p>`) +
    `</article>`
  );
}

/* Los cortes que PERDIERON, con sus números.
 *
 * Van plegados porque son nueve filas de estadística y no es lo que se viene a
 * mirar, pero van. Enseñar solo el ganador de una búsqueda entre nueve es la
 * forma más limpia de que un empate parezca un hallazgo: si el de 116 y el de
 * 147 separan casi lo mismo, eso se ve aquí y en ningún otro sitio. */
function plegableCandidatos(f) {
  const lista = f.candidatos || [];
  if (!lista.length) return "";
  return plegable(
    `Los ${lista.length} cortes que se han probado`,
    `<p class="explica">Cada fila es «y si el corte estuviera aquí». El ganador ` +
    `es el que más separa las dos mitades en valor absoluto, y la corrección se ` +
    `ha hecho sobre todos a la vez -y sobre la relación suave- para que sepa ` +
    `cuántas veces se ha mirado.</p>` +
    `<table class="tabla"><thead><tr><th>Corte</th><th>Debajo</th><th>Encima</th>` +
    `<th>Diferencia</th><th>p corregida</th></tr></thead><tbody>` +
    lista.map((c) => (
      `<tr${f.carga !== null && c.carga === f.carga ? ' class="parte"' : ""}>` +
      `<td>${num(c.carga, 0)}</td>` +
      `<td>${entero(c.n_debajo)}</td>` +
      `<td>${entero(c.n_encima)}</td>` +
      `<td>${num(c.diferencia)}</td>` +
      `<td>${num(c.p_corregida, 3)}` +
      `${c.significativa === true ? " ✓" : ""}</td>` +
      `</tr>`
    )).join("") +
    `</tbody></table>`,
  );
}

function seccionRecuperacion(rec, sinP) {
  const curvas = (rec && rec.curvas) || [];
  return (
    `<h2 class="grupo">Y cuánto dura</h2>` +
    `<p class="explica">${escapar(rec.nota)}</p>` +
    curvas.map(bloqueCurva).join("") +
    `<p class="aviso tenue">${escapar(sinP)}</p>`
  );
}

function bloqueCurva(c) {
  const dias = c.por_dia || [];
  return (
    `<article class="tarjeta">` +
    `<h3>${escapar(c.titulo)}</h3>` +
    (c.na
      ? bloqueNa(c.na)
      : barrasRecuperacion(c) +
        (c.lectura ? `<p class="lectura">${escapar(c.lectura)}</p>` : "") +
        // El día en que vuelve va aparte de la lectura aunque la lectura ya lo
        // diga: es el número que se busca, y en la frase está en medio.
        (c.vuelve_el_dia !== null && c.vuelve_el_dia !== undefined
          ? `<p class="cifra">De vuelta el día <b>+${entero(c.vuelve_el_dia)}</b></p>`
          : "") +
        `<table class="tabla"><thead><tr><th>Día</th><th>HRV</th>` +
        `<th>Salidas</th><th>Mitad central</th></tr></thead><tbody>` +
        dias.map((d) => (
          `<tr><td>+${entero(d.dia)}</td>` +
          (d.media === null || d.media === undefined
            ? `<td class="motivo" colspan="3">${escapar(d.na || "")}</td>`
            : `<td>${num(d.media)} ms</td><td>${entero(d.n)}</td>` +
              `<td>${num(d.p25)} a ${num(d.p75)}</td>`) +
          `</tr>`
        )).join("") +
        `</tbody></table>`) +
    `<p class="ficha">${cuenta(c.n_salidas, "salida aislada", "salidas aisladas")}` +
    `${c.desde_carga !== null && c.desde_carga !== undefined
      ? ` de ${num(c.desde_carga, 0)} de carga para arriba` : ""}</p>` +
    // El aviso de la curva ENTERA, no el de cada barra. Las cuatro barras salen
    // de las mismas salidas: son una foto de cuatro salidas mirada cuatro veces,
    // no cuatro medidas independientes.
    (c.aviso ? `<p class="aviso">${escapar(c.aviso)}</p>` : "") +
    `</article>`
  );
}

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

/* La ventana elegida se recuerda entre pantallas y entre arranques. Sin esto,
 * pasar de concordancia a impacto volvería a 180 días en silencio y las dos
 * pantallas hablarían de periodos distintos sin decirlo. */
const guardado = localStorage.getItem(RECUERDA_VENTANA);
if (guardado && [...$("dias").options].some((o) => o.value === guardado)) {
  $("dias").value = guardado;
}

$("dias").addEventListener("change", () => {
  try { localStorage.setItem(RECUERDA_VENTANA, $("dias").value); } catch { /* sin sitio */ }
  cargar();
});

window.addEventListener("hashchange", cargar);
cargar();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((e) => {
    console.warn("service worker no registrado:", e);
  });
}
