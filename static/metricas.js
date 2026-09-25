/*
 * Las ocho vistas de métricas.
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
 * Con la base vacía se pintan las ocho, enteras, con el motivo en cada casilla.
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
  calibracion: "/api/metrics/calibracion",
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
    // SIN SELECTOR DE VENTANA (25/09/2026). En la portada la ventana no es lo
    // fresco: es «lo normal para ti», la referencia contra la que se comparan
    // los últimos siete días. Pedirle a quien abre la aplicación que elija
    // entre 30 días y 2 años es enseñarle el aparato de medir antes que la
    // medida, que es justo lo que el endpoint de la portada dice que existe
    // para evitar. Se pide SIN `dias` y el servidor pone su referencia: así no
    // hay un 180 escrito aquí que un día acabe distinto del de la API.
    conVentana: false,
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
  // El título no dice «calibración» y el botón de abajo sí, y las dos cosas son
  // correctas. Abajo hace falta una palabra corta y suya; aquí hay sitio para
  // la pregunta que trae a esta pantalla, que no es «cómo va mi calibración»
  // sino «dónde no me fío». Y el subtítulo dice lo que NO es, porque es lo
  // primero que hay que saber de esta vista: no es una nota al motor.
  calibracion: {
    titulo: "Dónde el motor y tú no estáis de acuerdo",
    subtitulo: "Hacia qué lado tiras, en qué regla se concentra y cómo salió",
    pintar: pintarCalibracion,
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
  // `conVentana: false` quita el selector y NO manda `dias`: `pedir` descarta
  // los `undefined`, y el servidor aplica su ventana por defecto. Cualquier otra
  // vista sigue igual que siempre.
  const conVentana = v.conVentana !== false;
  $("barra-ventana").hidden = !conVentana;
  const dias = conVentana ? Number($("dias").value) : undefined;

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
  // El semáforo va PRIMERO y es lo único de la portada que es un gráfico. Antes
  // iba de tercera línea del segundo bloque, escrito, entre las sesiones de
  // fuerza y las salidas de bici: el resumen de la semana entera colocado como
  // un recuento más.
  //
  // «Qué ha cambiado» sube justo debajo (25/09/2026): los dos hablan de ESTA
  // semana, y estaban separados por dos bloques que miran otra cosa. Lo fresco
  // arriba y junto; lo que se sabe de fondo, después.
  const partes = [];
  partes.push(bloqueSemaforo(d.que_ha_cambiado));
  partes.push(bloqueQueHaCambiado(d.que_ha_cambiado));
  partes.push(bloqueComoVoy(d.como_voy));
  partes.push(bloqueApetecia(d.como_voy));
  partes.push(bloqueLoQueSeSabe(d.lo_que_se_sabe));
  partes.push(bloqueLoQueFalta(d.lo_que_no_se_puede_saber));

  // LA COBERTURA, AL FINAL Y PLEGADA. Iba la primera: cuatro rangos de fechas
  // -check-ins, Garmin, bici, fuerza- antes de un solo dato. Es lo que dice de
  // dónde sale cada número, y eso se consulta cuando un número extraña, no se
  // lee cada mañana antes de ver cómo va la semana. Plegada no desaparece: el
  // resumen dice qué hay dentro.
  partes.push(
    `<details class="de-donde"><summary>De dónde salen estos datos</summary>` +
    pintarCobertura(d.cobertura, d.ventana) +
    `</details>`,
  );

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
  /* PLEGADO, CON EL NÚMERO FUERA (25/09/2026).
   *
   * Eran tres tarjetas con «Llevas 8 y hacen falta 20 para que el número
   * signifique algo» al final de la pantalla que se abre cada día. Para quien
   * ha construido el sistema, útil; para cualquier otro, tres párrafos sobre
   * tamaños de muestra.
   *
   * Plegado NO es lo que este bloque tiene prohibido. Lo prohibido -escrito
   * arriba- es DESAPARECER en silencio, porque entonces el día bueno se lee
   * igual que el día roto. El resumen dice cuántas preguntas esperan datos, o
   * que no espera ninguna, y eso es exactamente la distinción que tiene que
   * seguir viéndose sin abrir nada. El `<h2>` va dentro del `<summary>`: la
   * sección sigue siendo una sección, y el arnés lo comprueba. */
  const n = (lista || []).length;
  const estado = n
    ? `<span class="cuantas-pendientes">${n}</span>`
    : `<span class="nada-pendiente">nada pendiente</span>`;
  const abre =
    `<details class="pendientes"><summary>` +
    `<h2 class="grupo">Lo que todavía no se puede contestar ${estado}</h2>` +
    `</summary>`;
  if (!n) {
    return (
      abre +
      `<p class="explica">Nada: las cuatro preguntas de esta pantalla ya tienen ` +
      `datos suficientes detrás. Si alguna vista sale vacía a partir de aquí, ` +
      `no es por falta de histórico.</p></details>`
    );
  }
  return (
    abre +
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
    )).join("") +
    `</details>`
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
        // La tabla ya NO se pinta aquí, y el motivo por el que se pintaba sigue
        // valiendo: el `na` de esta línea dice que la última SEMANA no se puede
        // situar dentro del histórico, y la tabla no habla de la semana, habla
        // de la ventana entera, así que desaparecer con la línea sería perderla
        // por un motivo que no es suyo. Lo que ha cambiado es que ahora la
        // recoge `bloqueApetecia`, que la busca por `l.tabla` sin mirar si la
        // línea tiene `na`: se sigue viendo, y se ve como un bloque entero.
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
    if (l.n_reciente) detalle.push(`medido en ${cuenta(l.n_reciente, "día", "días")}`);

    return (
      `<div class="linea-portada">` +
      `<div class="etiqueta-portada">${escapar(l.etiqueta)}</div>` +
      `<div class="lectura-portada ${clase}">${escapar(l.lectura)}</div>` +
      (detalle.length
        ? `<div class="ficha-portada">${escapar(detalle.join(" · "))}</div>`
        : "") +
      // Once de las doce líneas no la traen, y la que la trae no se entiende sin
      // ella. Va DENTRO de la línea y no al final del bloque para que se lea
      // pegada a su número y no como una tabla suelta al pie de la portada.
      `</div>`
    );
  }).join("");

  // EL QUESITO DE LOS TRES VEREDICTOS, Y LAS OCHO LÍNEAS DEBAJO DEL TRIÁNGULO.
  //
  // Las líneas no sobran ni están mal escritas -"por debajo de lo tuyo" se lee
  // sin traducir-, pero son ocho, y ocho veredictos seguidos hay que sumarlos
  // con la cabeza para contestar lo único que se le pregunta a este bloque: ¿voy
  // bien esta semana o no? Esa suma la hace el servidor en `resumen` y la enseña
  // el quesito de un vistazo.
  //
  // Bici y fuerza no están en el quesito -no comparan con nada- pero SÍ siguen
  // en la lista de dentro, que es donde se leen: "3 salidas esta semana, la
  // última hoy" es una frase completa que no necesita ningún veredicto.
  const r = b.resumen;
  const svg = quesito({
    trozos: [
      { etiqueta: "mejor", sub: "de lo tuyo", n: r.mejor, color: AZUL },
      { etiqueta: "como siempre", n: r.normal, color: TENUE },
      { etiqueta: "peor", sub: "de lo tuyo", n: r.peor, color: NARANJA },
    ],
    total: r.senales,
    unidadTotal: plural(r.senales, "señal", "señales"),
  });
  const detalle = `<article class="tarjeta portada">${filas}</article>`;
  if (!svg) return cabeceraBloque(b) + detalle;

  // La frase nombra el lado que más pesa. Si empatan lo dice: un empate es la
  // respuesta correcta a una semana que no tira para ningún lado, y forzar un
  // ganador sería inventarse una tendencia de una diferencia de cero.
  const frase = r.peor > r.mejor
    ? `De ${cuenta(r.senales, "señal", "señales")}, ${entero(r.peor)} ` +
      `${plural(r.peor, "va", "van")} peor de lo tuyo esta semana y ` +
      `${entero(r.mejor)} mejor.`
    : r.mejor > r.peor
      ? `De ${cuenta(r.senales, "señal", "señales")}, ${entero(r.mejor)} ` +
        `${plural(r.mejor, "va", "van")} mejor de lo tuyo esta semana y ` +
        `${entero(r.peor)} peor.`
      : `De ${cuenta(r.senales, "señal", "señales")}, ${entero(r.normal)} ` +
        `${plural(r.normal, "está", "están")} en tu rango de siempre.`;

  return bloqueDeVistazo(b.titulo, svg, frase, detalle);
}

/* LAS CUATRO CASILLAS DE «¿TE APETECÍA?» CONTRA «¿FUISTE?», EN UN QUESITO.
 *
 * Esto vivía dentro de la línea de discordancia de «Cómo voy», como una tabla de
 * cuatro filas con su título, su lectura y su ficha: noventa palabras metidas
 * entre dos veredictos de una línea. Y tenía que estar a la vista, por el motivo
 * que sigue escrito en `tablaDiscordancia`: el número solo -«el 30 % de los días
 * no coincidieron»- junta dos cosas opuestas y los dos repartos extremos dan el
 * mismo 30 % describiendo a dos personas distintas.
 *
 * El gráfico resuelve las dos cosas a la vez: el reparto que la tabla contaba
 * con números se VE, y la tabla se va al detalle sin dejar ningún porcentaje
 * huérfano arriba.
 *
 * Y son BARRAS y no un quesito, aunque cuatro casillas excluyentes que suman los
 * días con las dos contestadas sean exactamente una tarta. El motivo es el
 * rótulo: la leyenda del quesito va a la derecha del anillo, con menos de la
 * mitad del ancho, y «No te apetecía y no entrenaste» se sale del dibujo. En las
 * barras tumbadas la etiqueta va ENCIMA de su barra y tiene el ancho entero. Un
 * quesito con los rótulos cortados no es más visual que una tabla: es una tabla
 * ilegible con un círculo al lado.
 *
 * Sale de «Cómo voy» y se pone al lado porque ya no es una línea de ese bloque:
 * es una pregunta entera, con su título, que el servidor manda dentro de la
 * línea de discordancia por dónde se calcula, no por dónde se lee.
 */
function bloqueApetecia(b) {
  const l = (b && b.lineas || []).find((x) => x.tabla);
  if (!l) return "";
  const t = l.tabla;
  if (t.na) {
    return (
      `<section class="bloque-vistazo"><h2>${escapar(t.titulo)}</h2>` +
      bloqueNa(t.na) + `<p class="ficha">${escapar(t.ficha)}</p></section>`
    );
  }

  // El color separa lo que coincidió de lo que no, y eso lo decide el servidor
  // en `discordante`: la misma regla que pinta la fila de la tabla, no una copia
  // hecha aquí comparando `apetece` con `voy`. Va por `alReves` porque es
  // justamente eso -azul lo que cuadra, naranja lo que no- casilla a casilla.
  const svg = barrasTumbadas({
    valores: t.celdas.map((c) => c.n),
    rotulos: t.celdas.map((c) => entero(c.n)),
    etiquetas: t.celdas.map((c) => c.etiqueta),
    alReves: t.celdas.map((c) => c.discordante),
    pie: `días, de ${entero(t.n)} con las dos contestadas`,
  });
  if (!svg) return "";

  return bloqueDeVistazo(
    t.titulo, svg,
    t.lectura || `De ${cuenta(t.n, "día", "días")} con las dos contestadas, ` +
      `${entero(t.discordantes)} no ${plural(t.discordantes, "coincidió", "coincidieron")}.`,
    tablaDiscordancia(t),
  );
}

/* EL SEMÁFORO DE LA SEMANA, en un quesito, y lo primero de todo.
 *
 * Es el único número de este proyecto que resume lo que el sistema DECIDIÓ, y
 * no lo que midió: cuántos días te dijo verde, cuántos ámbar y cuántos rojo. Y
 * estaba escrito en una línea de texto -«3 verdes, 1 ámbar esta semana»- metida
 * entre «Sesiones de fuerza» y «Salidas de bici», o sea con el mismo peso
 * visual que un recuento de entrenos.
 *
 * El quesito es el gráfico que pidió para esto, por su nombre. Y tiene una
 * ventaja sobre la frase que no es de estilo: el trozo enseña la PROPORCIÓN. «3
 * verdes y 1 ámbar» y «6 verdes y 2 ámbares» son la misma frase con otros
 * números y el mismo dibujo, que es exactamente lo que hay que ver.
 *
 * El total del agujero lo manda el servidor en `dias_con_decision`. Sumar aquí
 * los tres colores habría sido más corto y habría metido en el navegador el
 * primer número calculado de la portada.
 *
 * Si no hay ni un día con decisión no se pinta nada: un anillo vacío con un
 * cero en medio no dice «esta semana no hubo decisiones», dice «el gráfico está
 * roto». Eso lo cuenta `bloqueQueHaCambiado` con palabras, que es donde se
 * cuenta.
 */
function bloqueSemaforo(b) {
  const l = (b && b.lineas || []).find((x) => x.clave === "semaforo");
  if (!l || !l.esta_semana || !l.dias_con_decision) return "";
  const s = l.esta_semana;
  const a = l.semana_anterior || {};

  const svg = quesito({
    trozos: [
      { etiqueta: plural(s.green, "verde", "verdes"), n: s.green, color: COLOR_LUZ.green },
      { etiqueta: plural(s.amber, "ámbar", "ámbares"), n: s.amber, color: COLOR_LUZ.amber },
      { etiqueta: plural(s.red, "rojo", "rojos"), n: s.red, color: COLOR_LUZ.red },
    ],
    total: l.dias_con_decision,
    unidadTotal: plural(l.dias_con_decision, "día decidido", "días decididos"),
  });
  if (!svg) return "";

  return bloqueDeVistazo(
    "El semáforo de esta semana",
    svg,
    l.lectura,
    `<table class="tabla"><thead><tr><th>Color</th><th>Esta semana</th>` +
    `<th>La anterior</th></tr></thead><tbody>` +
    ["green", "amber", "red"].map((c) => (
      `<tr><td>${escapar(NOMBRE_LUZ_PWA[c])}</td>` +
      `<td>${entero(s[c] || 0)}</td><td>${entero(a[c] || 0)}</td></tr>`
    )).join("") +
    `</tbody></table>`,
  );
}

// Los nombres son los mismos que los del servidor y están escritos dos veces, y
// eso NO es un descuido: aquí solo se usan para la cabecera de una tabla del
// detalle, donde no hay número al lado que concuerde. Las frases que sí llevan
// número las escribe el servidor, que es quien tiene el singular y el plural.
const NOMBRE_LUZ_PWA = { green: "verdes", amber: "ámbares", red: "rojos" };

function bloqueQueHaCambiado(b) {
  if (!b) return "";
  if (b.estado === "vacio" || !b.lineas || !b.lineas.length) {
    return cabeceraBloque(b) + bloqueNa(b.na);
  }

  // El semáforo ya está dibujado arriba del todo. Repetirlo aquí escrito es la
  // forma más fácil de que la portada vuelva a tener dos veces lo mismo, que es
  // de donde viene la mitad de sus mil palabras.
  const filas = b.lineas.filter((l) => l.clave !== "semaforo").map((l) => (
    `<div class="linea-portada">` +
    `<div class="etiqueta-portada">${escapar(l.etiqueta)}</div>` +
    `<div class="lectura-portada">${escapar(l.lectura)}</div>` +
    `</div>`
  )).join("");
  // Puede quedarse sin ninguna: el semáforo es la única línea que el servidor
  // manda siempre que haya UNA decisión, y los dos recuentos de entrenos pueden
  // no venir. Un título con una tarjeta vacía debajo se lee como un fallo.
  if (!filas) return "";

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
  // «en Más» y no «en las vistas de abajo», que es lo que ponía. Desde el
  // 25/09/2026 las vistas técnicas ya no están en la barra de abajo: están
  // detrás de «Más». La frase se quedó apuntando a un sitio donde ya no había
  // nada, y es la que se lee en la pantalla que se abre a diario.
  if (b.resto) {
    pie.push(
      `<p class="explica">Hay ${cuenta(b.resto, "hallazgo", "hallazgos")} más, ` +
      `que éstos. Están enteros en <a href="/avanzado.html">Más</a>.</p>`,
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

  /* LA FICHA SE PLIEGA. Lo de dentro NO se toca.
   *
   * Siguen `r` con su nombre de una letra, la p corregida con sus cuatro
   * decimales, los pares comparados y el método. Y siguen por lo que estaba
   * escrito aquí: «r» es la fuerza CON SIGNO en una escala fija, ya dicha en
   * castellano arriba en la frase y en la marca de fuerza, y lo que hace la
   * cifra es dejar COMPROBAR la frase. Para comprobar hace falta el número, no
   * otra paráfrasis del número.
   *
   * Lo que cambia es dónde. Esta es la primera pantalla, la puerta, y llevaba
   * cinco hallazgos con «r = −0,38 · p = 0,0002 (corregida) · 175 pares
   * comparados · correlación de rangos» debajo de cada uno: veinte cifras
   * técnicas en la vista que se abre a diario. Plegada, la frase se lee sola y
   * el número está a un toque para quien quiera comprobarlo, que son dos
   * momentos distintos y nunca el mismo.
   *
   * El plegable es por tarjeta y no uno al final del bloque. Uno al final
   * obligaría a emparejar cinco fichas con cinco frases contando de arriba
   * abajo, y una comprobación que hay que emparejar a mano ya no comprueba
   * nada. */
  const detalle = [];
  if (f.dias_despues !== null && f.dias_despues !== undefined) {
    detalle.push(`${cuenta(f.dias_despues, "día", "días")} después`);
  }
  if (f.r !== null && f.r !== undefined) detalle.push(`r = ${num(f.r)}`);
  if (f.p_corregida !== null && f.p_corregida !== undefined) {
    detalle.push(`p = ${num(f.p_corregida, 4)} (corregida)`);
  }
  if (f.n) detalle.push(`${entero(f.n)} pares comparados`);
  if (f.metodo) detalle.push(escapar(METODOS[f.metodo] || f.metodo));

  /* LA FRASE SOLA FUERA, Y LAS TRES PROSAS DENTRO.
   *
   * Cada hallazgo llevaba cuatro párrafos en la primera pantalla: la frase, el
   * matiz de la forma de la curva, la nota de lo que confirma -que lista cinco
   * exposiciones con sus paréntesis: cuarenta palabras- y la de lo que discrepa.
   * Por cinco tarjetas, la mitad de la portada. La frase es el hallazgo; las
   * otras tres son cómo se sabe y cuánto fiarse, que se leen en otro momento.
   *
   * PERO LA ADVERTENCIA NO DESAPARECE DE FUERA, y eso era lo que estaba escrito
   * aquí y sigue valiendo: «enterrarlo en un plegable deja la frase sola y
   * pareciendo más segura de lo que es». Lo que se va dentro es la REDACCIÓN
   * larga; fuera se queda la marca corta -«lo mismo sale por otros 5 caminos»,
   * «ojo: 2 apuntan al revés»-, que avisa en seis palabras de lo que la de
   * cuarenta avisaba, y el texto entero está a un toque.
   *
   * Los dos números son la longitud de dos listas que manda el servidor. No hay
   * más cuenta que ésa, y la hay porque la frase corta necesita decir cuántos
   * son para no ser un «ojo» sin tamaño.
   */
  const dentro =
    (h.matiz ? `<p class="matiz">${escapar(h.matiz)}</p>` : "") +
    (h.nota_confirmacion
      ? `<p class="confirma">${escapar(h.nota_confirmacion)}</p>` : "") +
    (h.nota_discrepancia
      ? `<p class="discrepa">${escapar(h.nota_discrepancia)}</p>` : "") +
    (detalle.length ? `<p class="ficha">${escapar(detalle.join(" · "))}</p>` : "");

  const confirman = (h.confirmada_por || []).length;
  const discrepan = (h.discrepa || []).length;

  return (
    `<article class="tarjeta hallazgo">` +
    `<p class="grupo-hallazgo">${escapar(h.grupo.titulo)} · decide ` +
    `${escapar(h.grupo.decision)}</p>` +
    `<p class="frase ${clase}">${escapar(h.frase)}${marca}</p>` +
    (confirman
      // `cuenta()` no vale aquí: pone el número delante y sale "por 5 otros
      // caminos". El numeral va en medio -"por otros 5 caminos"- y con uno se
      // cae entero, así que las dos formas se escriben a mano.
      ? `<p class="confirma">Lo mismo sale por ` +
        `${confirman === 1 ? "otro camino" : `otros ${entero(confirman)} caminos`}.</p>`
      : "") +
    (discrepan
      ? `<p class="discrepa">Ojo: ${cuenta(discrepan, "señal apunta", "señales apuntan")} ` +
        `al revés.</p>`
      : "") +
    (dentro ? plegable("ver detalle", dentro) : "") +
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

  partes.push(bloqueLoQueNotas(d.pares, d.resumen_pares));

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

/* LAS SIETE HIPÓTESIS, EN SIETE BARRAS. La pregunta de la vista, de un vistazo.
 *
 * Esta vista abría con un párrafo de ocho líneas explicando qué es una barra de
 * −1 a 1 y por qué estas siete no llevan corrección, y después SIETE tarjetas
 * con su «r = −0.31 · p = 0.004» cada una. Para contestar «¿lo que noto se
 * parece a lo que mide el reloj?» había que leer siete veces y sumar de cabeza.
 *
 * Las siete barras juntas contestan eso solo: azul es que va por donde debía,
 * naranja que va al contrario, gris que no hay señal. La forma de la lista es la
 * respuesta antes de leer un número.
 *
 * LO DE DENTRO NO SE BORRA. La r, la p, el método, los días emparejados y el
 * aviso de muestra corta siguen enteros, con su tarjeta cada uno, detrás de «ver
 * detalle». La regla 2 dice que eso no puede estar en la pantalla que se abre;
 * no dice que sobre, y aquí hay una guarda -`DETALLE_OBLIGADO` en
 * `tests/render_pwa.mjs`- que falla si alguien decide que la forma barata de
 * pasar la regla es tirarlo.
 */
function bloqueLoQueNotas(pares, resumen) {
  const svg = barrasTumbadas({
    valores: pares.map((p) => p.r),
    rotulos: pares.map((p) => num(p.r)),
    etiquetas: pares.map((p) => p.titulo),
    alReves: pares.map((p) => p.al_reves),
    pie: "cada pareja, de −1 a +1",
  });

  const detalle =
    `<p class="explica">Cada pareja junta algo que contestas por la mañana con ` +
    `algo que el reloj mide solo. La barra va de −1 a 1 con el cero marcado, y ` +
    `<b>el signo que se espera</b> está escrito debajo: lo interesante no es que ` +
    `la correlación sea alta, es que vaya en la dirección que debería. Estas ` +
    `siete son preguntas hechas de antemano, así que no llevan corrección por ` +
    `comparaciones múltiples: no hay una rejilla que rastrear, hay siete ` +
    `hipótesis. El bloque de abajo sí la lleva, y por eso.</p>` +
    pares.map((p) => (
      `<article class="tarjeta">` +
      `<h3>${escapar(p.titulo)}</h3>` +
      `<p class="sub">${escapar(p.etiqueta_x)} · ${escapar(p.etiqueta_y)}</p>` +
      barraR(p) +
      `<p class="cifra">r = <b>${num(p.r)}</b>${p.p !== null && p.p !== undefined
        ? ` · p = ${num(p.p, 3)}` : ""}</p>` +
      (p.lectura
        ? `<p class="lectura">${escapar(p.lectura)}</p>`
        : bloqueNa(p.na)) +
      `<p class="esperado">Se espera que ` +
      `${p.signo_esperado > 0 ? "suban juntos" : "vaya uno al revés del otro"}.</p>` +
      ficha(p) +
      (p.aviso && p.lectura ? `<p class="na">${escapar(p.aviso)}</p>` : "") +
      `</article>`
    )).join("");

  const r = resumen;
  const frase = r.calculadas
    ? `De ${cuenta(r.calculadas, "pareja con número", "parejas con número")}, ` +
      `${entero(r.como_se_esperaba)} ${plural(r.como_se_esperaba, "va", "van")} ` +
      `por donde debía y ${entero(r.al_reves)} al contrario` +
      `${r.sin_signo_claro
        ? `; ${plural(r.sin_signo_claro, "la otra se queda", "las otras se quedan")} ` +
          `en nada` : ""}.`
    : `Todavía no hay días suficientes para calcular ni una de las ` +
      `${entero(r.parejas)} parejas.`;

  if (!svg) {
    return (
      `<article class="tarjeta"><h2>Lo que notas frente al reloj</h2>` +
      bloqueNa(frase) + plegable("ver detalle", detalle) + `</article>`
    );
  }

  return bloqueDeVistazo("Lo que notas frente al reloj", svg, frase, detalle);
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
 * ESA PARTICIÓN SIGUE MANDANDO, y ahora manda en el dibujo. Las barras van en
 * dos tandas -primero las independientes, después las que comparten origen- con
 * su pie diciendo cuál es cuál, así que el reparto se ve sin leer los títulos.
 * Un solo gráfico con las diez ordenadas por tamaño volvería a la lista de la
 * que se sale creyendo que el reloj es coherentísimo.
 *
 * Y ninguna tarjeta se borra: las diez siguen enteras, con su r, su p corregida
 * y su aviso de origen compartido dentro, detrás de «ver detalle».
 */
function seccionInternas(d) {
  const r = d.resumen_internas;
  const indep = d.internas.filter((c) => c.mismo_origen === null);
  const compartidas = d.internas.filter((c) => c.mismo_origen !== null);

  const barras = (lista, pie) => barrasTumbadas({
    valores: lista.map((c) => c.r),
    rotulos: lista.map((c) => num(c.r)),
    etiquetas: lista.map((c) => c.titulo),
    alReves: lista.map((c) => c.al_reves),
    pie,
  });

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

  const detalle =
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
    );

  const svg =
    barras(indep, "las que el reloj mide por separado") +
    barras(compartidas, "las que el reloj calcula una a partir de la otra");

  if (!svg) {
    return (
      `<article class="tarjeta"><h2>El reloj consigo mismo</h2>` +
      bloqueNa("todavía no hay días suficientes para cruzar ni una pareja") +
      plegable("ver detalle", detalle) + `</article>`
    );
  }

  // La frase se queda en las INDEPENDIENTES a propósito. Es la única cifra del
  // bloque que puede estar diciendo algo del cuerpo: las otras ocho salen altas
  // porque Garmin construye un número con el otro, y ponerlas en la frase de
  // debajo del gráfico sería dar por hecha justo la conclusión que este bloque
  // existe para dejar mirar.
  //
  // El cero se dice «ninguna» y no «0». Es la única cifra del panel que se
  // escribe con letra, y no por adorno: «0 aguantan la prueba» obliga a parar
  // medio segundo a decidir si ese cero es un resultado o un dato que falta, y
  // aquí es un resultado -se han mirado las dos y no ha salido ninguna-. Con el
  // resto de números no pasa, porque van dentro del gráfico y la barra ya dice
  // si hay algo o no.
  const sig = r.independientes.significativas;
  const frase =
    `De las ${entero(r.independientes.parejas)} parejas que el reloj mide por ` +
    `separado, ${sig ? entero(sig) : "ninguna"} ` +
    `${plural(sig, "aguanta", "aguantan")} la prueba; ` +
    `las otras ${entero(r.comparten_origen.parejas)} las calcula una con la otra.`;

  return bloqueDeVistazo("El reloj consigo mismo", svg, frase, detalle);
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

  partes.push(bloqueDondePican(d));

  $("vista").innerHTML = partes.join("");
}

/* EL MONTÓN DE PICOS, EN SIETE BARRAS.
 *
 * Esta vista tenía cincuenta tarjetas iguales en la pantalla principal, cada una
 * con su curva, su «Pico en ±3 días» y su línea de lectura: mil seiscientas
 * palabras para contestar una pregunta de sí o no. La pregunta es si lo que
 * notas va por delante del reloj o por detrás, y la contesta el sitio donde se
 * amontonan los picos, no ninguna pareja suelta.
 *
 * Así que el histograma va arriba y grande -una barra por retardo, del -3 al
 * +3-, y las cincuenta tarjetas siguen enteras detrás del triángulo. Ninguna se
 * borra, incluidas las que no pican: el bloque de «Sin pico todavía» se va con
 * su párrafo dentro, porque el motivo de que estén ahí no cambia por plegarlas.
 *
 * Las alturas y los tres totales de la frase vienen de `reparto_desfases`. Aquí
 * no se cuenta nada; el único reparto que se hace en esta función es separar las
 * tarjetas en dos listas para el detalle, y esas listas no producen ninguna
 * cifra que se lea.
 */
function bloqueDondePican(d) {
  const r = d.reparto_desfases;
  const rango = d.rango_desfase;

  const conPico = d.rejilla.filter((f) => f.mejor_desfase !== null && f.mejor_desfase !== undefined);
  const sinPico = d.rejilla.filter((f) => f.mejor_desfase === null || f.mejor_desfase === undefined);
  conPico.sort((a, b) => Math.abs(b.mejor_desfase) - Math.abs(a.mejor_desfase));

  const tarjeta = (f) => (
    `<article class="tarjeta">` +
    `<h3>${escapar(f.etiqueta_x)} · ${escapar(f.etiqueta_y)}</h3>` +
    curvaDesfase(f.por_desfase, f.mejor_desfase) +
    (f.mejor_desfase !== null && f.mejor_desfase !== undefined
      ? `<p class="cifra">Pico en <b>${f.mejor_desfase > 0 ? "+" : ""}` +
        `${cuenta(f.mejor_desfase, "día", "días")}</b></p>`
      : "") +
    (f.lectura ? `<p class="lectura">${escapar(f.lectura)}</p>` : bloqueNa(f.na)) +
    plegable("Los siete desfases, uno a uno", tablaDesfases(f.por_desfase)) +
    `</article>`
  );

  // Los dos números de los encabezados salen de `r` y no de `conPico.length`.
  //
  // El `filter` de arriba puede quedarse: parte la lista en dos para pintarlas
  // en dos grupos, y eso es ordenar, no medir. Pero el número entre paréntesis
  // SE LEE, y una cifra que se lee no sale de una cuenta hecha en el móvil: el
  // servidor ya manda `con_pico` y `sin_pico` contados con su mismo criterio, y
  // contarlos otra vez aquí sería tener dos definiciones de «pareja con pico»
  // -la del encabezado y la de la frase de fuera- esperando a no coincidir.
  let detalle = `<p class="explica">${escapar(d.convenio)}</p>`;
  if (conPico.length) {
    detalle += `<h3 class="grupo">Con pico calculado (${entero(r.con_pico)})</h3>`;
    detalle += conPico.map(tarjeta).join("");
  }
  if (sinPico.length) {
    detalle +=
      `<h3 class="grupo">Sin pico todavía (${entero(r.sin_pico)})</h3>` +
      `<p class="explica">Siguen aquí a propósito. Una pantalla que solo enseña ` +
      `las parejas que salieron parece decir más de lo que sabe.</p>` +
      sinPico.map(tarjeta).join("");
  }

  // EL EJE: siete números y una sola línea de palabras debajo, en el pie.
  //
  // La tentación era poner «3 días antes» debajo de cada barra, que explica el
  // convenio de signos sin que haya que recordarlo. No cabe: siete barras en 340
  // puntos de ancho dan cuarenta por barra, y «días después» ocupa sesenta, así
  // que los rótulos se pisan unos a otros. Un eje ilegible incumple la regla 3
  // más de lo que la cumple la palabra.
  //
  // Así que los números van solos -con su signo, que es lo que la regla 3 pide-
  // y la dirección se dice UNA vez en el pie, que tiene el ancho entero.
  const retardos = [];
  for (let k = rango[0]; k <= rango[1]; k++) retardos.push(k);
  const svg = barrasDeVistazo({
    valores: retardos.map((k) => r.por_desfase[String(k)]),
    rotulos: retardos.map((k) => entero(r.por_desfase[String(k)])),
    etiquetas: retardos.map((k) => [k > 0 ? `+${k}` : conMenos(String(k))]),
    pie: "← notas después · días · notas antes →",
    unidad: "parejas",
  });

  // La frase compara los dos lados del cero y nombra el que gana, sin decir por
  // cuánto: «21 contra 14» invita a restar y a creerse la resta. Si van igualadas
  // lo dice y ya está, que es la respuesta honesta a una pantalla que todavía no
  // ha visto bastantes días.
  //
  // LAS QUE PICAN EN EL CERO SE NOMBRAN, aunque no ayuden a decidir. La frase
  // decía «de 50 con pico, 31 después y 10 antes» y quien sumara encontraba
  // nueve parejas que no estaban en ninguna de las dos cifras. Esas nueve son
  // la barra más alta del centro del dibujo, así que el número que falta está
  // ahí pintado a tamaño grande: una frase que no lo menciona no es escueta,
  // es una frase a la que le falta el sumando que el lector tiene delante.
  const deLas = cuenta(r.con_pico, "pareja con pico", "parejas con pico");
  const aLaVez = r.a_la_vez ? ` y ${entero(r.a_la_vez)} a la vez` : "";
  const frase = !r.con_pico
    ? `Ninguna de las ${entero(r.parejas)} parejas tiene todavía días suficientes ` +
      `para saber dónde pica.`
    : r.se_adelanta > r.va_detras
      ? `De ${deLas}, ${entero(r.se_adelanta)} ` +
        `${plural(r.se_adelanta, "pica", "pican")} antes que el reloj, ` +
        `${entero(r.va_detras)} después${aLaVez}: lo que notas se adelanta.`
      : r.va_detras > r.se_adelanta
        ? `De ${deLas}, ${entero(r.va_detras)} ` +
          `${plural(r.va_detras, "pica", "pican")} después que el reloj, ` +
          `${entero(r.se_adelanta)} antes${aLaVez}: lo que notas va por detrás.`
        : `De ${deLas}, van ${entero(r.se_adelanta)} por delante, ` +
          `${entero(r.va_detras)} por detrás${aLaVez}: de momento no tira para ` +
          `ningún lado.`;

  if (!svg) {
    return (
      `<section class="bloque-vistazo"><h2>Lo que notas y lo que marca el reloj</h2>` +
      `<p class="frase-vistazo">${escapar(frase)}</p>` +
      plegable("ver detalle", detalle) + `</section>`
    );
  }
  return bloqueDeVistazo("Lo que notas y lo que marca el reloj", svg, frase, detalle);
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
  /* El aviso de la causa se queda ARRIBA y se queda CORTO.
   *
   * La versión larga -la que manda el servidor en `advertencia`, con el ejemplo
   * del peso muerto y la lumbar- baja al detalle del primer bloque. No por la
   * regla 2, aunque también la incumpla al escribir «correlacionado»: por la 4.
   * Son seis líneas de párrafo encima del gráfico, y lo que se pidió es que
   * debajo del gráfico haya UNA frase; encima no puede haber seis.
   *
   * Lo que no se puede hacer es quitarlo del todo. Esta vista ordena cosas que
   * el usuario hace y les pone al lado cuánto le baja la HRV: sin el aviso, la
   * lectura natural es «el día 1 me sienta mal», y eso es exactamente lo que los
   * datos no pueden decir. El aviso corto dice lo mismo en una línea. */
  partes.push(
    `<p class="aviso ojo"><strong>Esto dice por dónde mirar, no quién tiene ` +
    `la culpa.</strong> Lo que entrenas va junto el mismo día y esta vista no ` +
    `puede separarlo.</p>`,
  );

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
    // Una línea y no tres. Decía lo mismo en tres frases -qué es el paréntesis,
    // por qué salen las vacías, que el sistema sabe mirarlas- encima del gráfico
    // y antes de él. La regla 1 pone el texto DEBAJO y en una frase; un
    // desplegable necesita su pie, pero le basta con uno.
    `<p class="explica">Entre paréntesis, cuántas cosas se han podido cruzar ` +
    `con ella.</p>` +
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
  const rank = elegido.respuesta
    ? await pedir(RUTAS.ranking, { dias, respuesta: elegido.respuesta })
    : null;

  partes.push(bloqueQueTeHaceCadaCosa(d, filas));
  partes.push(bloqueCuantoDura(d, filas, rank));

  $("vista").innerHTML = partes.join("");

  $("resp").addEventListener("change", (ev) => {
    elegido.respuesta = ev.target.value;
    cargar();
  });
}

const NOMBRE_FAMILIA = {
  bici: "Salidas de bici",
  rutina: "Rutinas de fuerza",
  fuerza: "Volumen y series",
  ejercicio: "Ejercicios sueltos",
};

/* La rejilla entera, agrupada, para el detalle. Es lo que ANTES era la vista.
 *
 * No se ha borrado ni una tarjeta: siguen la r, las medias de los dos grupos, la
 * p corregida y el veredicto de la corrección, palabra por palabra. Lo único que
 * ha cambiado es que hay que tocar «ver detalle» para verlas.
 *
 * Y esa es la diferencia que importa. Borrarlas habría sido la forma barata de
 * cumplir la regla 2 -sin números raros no hay números raros que esconder- y
 * habría tirado lo único que distingue a este panel de una app de fitness. Lo
 * que se pidió fue «si lo quieres conservar, que esté escondido detrás de un ver
 * detalle», que es conservarlo. */
function tarjetasPorFamilia(filas) {
  const familias = {};
  for (const f of filas) (familias[f.exposicion.familia] ||= []).push(f);
  return Object.entries(familias).map(([fam, lista]) => (
    `<h3 class="grupo">${escapar(NOMBRE_FAMILIA[fam] || fam)}</h3>` +
    lista.map(tarjetaImpacto).join("")
  )).join("");
}

function casillaDe(f, k) {
  return (f.por_dia || []).find((c) => c.dias_despues === k) || null;
}

/* LAS FILAS QUE TIENEN ALGO NORMAL QUE ENSEÑAR, ordenadas por cuánto se nota.
 *
 * Una exposición `binaria` -salí o no salí- parte los días en dos grupos y el
 * servidor manda la media de cada uno y su `diferencia`. Esa resta está en la
 * unidad de la respuesta -milisegundos de HRV, puntos de cansancio- y se lee
 * sola: «los días después de una salida larga tu HRV es nueve milisegundos más
 * baja». Eso es un número normal y va al gráfico.
 *
 * Una exposición `continua` -los kilos que moviste- no tiene dos grupos, y lo
 * único que el servidor puede decir de ella es una correlación. Una correlación
 * no es un número normal: hay que saber qué es para leerla, y la regla 2 la
 * manda al detalle. Así que las continuas NO entran en el gráfico. No se
 * esconden -van enteras en «ver detalle», con su `lectura` en castellano-, pero
 * no se les inventa una barra: dibujar una r de −0,38 al lado de una diferencia
 * de −9,2 ms pondría en el mismo eje dos cosas que no se miden igual, que es
 * peor que no dibujarla.
 *
 * Se exige `suficiente` además de `diferencia`: hay filas con dos días medidos
 * cuya resta de medias sale «2,0» y no significa nada. Dibujarla la pondría
 * arriba del todo, porque con dos datos cualquier diferencia es enorme. */
function loQueSeNota(d, filas) {
  const k = d.retardos[0];
  return filas
    // El filtro por `tipo` va PRIMERO y no da igual el orden. Una casilla de
    // exposición continua no trae la clave `diferencia`: no la trae a `null`,
    // no la trae. Preguntarle por ella da `undefined` -en JavaScript leer lo que
    // no existe no falla- y el `filter` de abajo la descartaría igual, con lo
    // que el resultado sería correcto por casualidad. Lo caza el `Proxy` de
    // `tests/render_pwa.mjs`, que apunta las lecturas de claves ausentes
    // precisamente porque el síntoma no se ve.
    .filter((f) => f.exposicion.tipo === "binaria")
    .map((f) => ({ f, c: casillaDe(f, k) }))
    .filter(({ c }) => (
      c && c.suficiente && c.diferencia !== null && c.diferencia !== undefined
    ))
    .sort((a, b) => Math.abs(b.c.diferencia) - Math.abs(a.c.diferencia));
}

function unidadDe(resp) {
  return resp.sufijo || (resp.unidad && resp.unidad !== "1-5" ? resp.unidad : "");
}

/* PRIMER BLOQUE: una barra por cosa que haces, en la unidad de la respuesta.
 *
 * Tumbadas y ordenadas por cuánto se notan, con el cero dibujado. Lo que se ve
 * sin leer es que las cinco de arriba son de bici y apuntan todas al mismo lado,
 * y eso antes había que sacarlo comparando dieciséis tarjetas con tres
 * correlaciones cada una. */
function bloqueQueTeHaceCadaCosa(d, filas) {
  if (!filas.length) return "";
  const resp = filas[0].respuesta;
  const k = d.retardos[0];
  const suf = unidadDe(resp);
  const titulo = `Qué le hace cada cosa a ${resp.en_frase || resp.etiqueta}`;
  const detalle =
    `<p class="explica">${escapar(d.advertencia)}</p>` + tarjetasPorFamilia(filas);

  const lista = loQueSeNota(d, filas);
  if (!lista.length) {
    return (
      `<section class="bloque-vistazo"><h2>${escapar(titulo)}</h2>` +
      bloqueNa(
        `ninguna de las ${filas.length} cosas que se cruzan con ` +
        `${resp.en_frase || resp.etiqueta} parte los días en dos grupos con ` +
        `bastantes días en cada uno, que es lo que hace falta para poder decir ` +
        `«tantos ${suf || "puntos"} de diferencia». Lo que sí hay está abajo`,
      ) +
      plegable("ver detalle", detalle) +
      `</section>`
    );
  }

  const peor = lista[0];
  const dif = peor.c.diferencia;
  // El valor absoluto es para poder escribir «baja 9,2» en vez de «cambia −9,2»,
  // que es la misma cifra dicha como se dice en voz alta. La palabra y el signo
  // salen del mismo sitio, así que no pueden contradecirse.
  const frase =
    `Lo que más se nota es «${peor.f.exposicion.etiqueta}»: ` +
    `${resp.en_frase || resp.etiqueta} ${dif < 0 ? "baja" : "sube"} ` +
    `${num(Math.abs(dif), 1)}${suf ? ` ${suf}` : ""} ` +
    `${k === 1 ? "al día siguiente" : `${k} días después`}.`;

  return bloqueDeVistazo(
    titulo,
    barrasTumbadas({
      valores: lista.map(({ c }) => c.diferencia),
      rotulos: lista.map(({ c }) => num(c.diferencia, 1)),
      etiquetas: lista.map(({ f }) => f.exposicion.etiqueta),
      pie: `${suf ? `${suf} · ` : ""}${k === 1 ? "al día siguiente" : `a los ${k} días`}`,
      positivoEsBueno: resp.sentido !== "alto_peor",
    }),
    frase,
    detalle,
  );
}

/* SEGUNDO BLOQUE: y eso, ¿cuánto dura?
 *
 * La misma resta de la de arriba del todo, pero a uno, dos y tres días. Es la
 * pregunta que la vista tenía contestada desde siempre -el servidor calcula los
 * tres retardos- y que no se podía leer: estaba repartida en tres cajitas
 * dentro de cada tarjeta, cada una con su r y su p, y para ver si subía o
 * bajaba había que ir apuntando números.
 *
 * La frase de debajo es la `lectura` del servidor tal cual. No se recompone
 * aquí: es la misma que lleva escribiendo esta vista desde el principio, y ya
 * dice «al día +2 ya está como siempre» en castellano. */
function bloqueCuantoDura(d, filas, rank) {
  const lista = loQueSeNota(d, filas);
  if (!lista.length) return rank ? seccionRanking(rank) : "";
  const { f } = lista[0];
  const resp = f.respuesta;
  const suf = unidadDe(resp);

  const casillas = d.retardos.map((k) => casillaDe(f, k));
  const valores = casillas.map((c) => (
    c && c.suficiente && c.diferencia !== null && c.diferencia !== undefined
      ? c.diferencia
      : null
  ));
  if (!valores.some((v) => v !== null)) return rank ? seccionRanking(rank) : "";

  return bloqueDeVistazo(
    `Cuánto dura lo de «${f.exposicion.etiqueta}»`,
    barrasDeVistazo({
      valores,
      rotulos: valores.map((v) => (v === null ? "" : num(v, 1))),
      etiquetas: d.retardos.map((k) => [`+${entero(k)}`, plural(k, "día", "días")]),
      pie: `${suf ? `${suf} · ` : ""}días después`,
      unidad: suf,
    }),
    f.lectura ? `${f.exposicion.etiqueta}: ${f.lectura}.` : "",
    // El ranking de los 38 ejercicios entero, con su resumen de la corrección,
    // cuelga de aquí: es la misma pregunta -cuánto dura y en qué se nota- mirada
    // ejercicio a ejercicio, y era el otro sitio de esta vista donde había una
    // tabla de correlaciones en la pantalla principal.
    rank ? seccionRanking(rank) : "",
  );
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

  // EL REPARTO DE LUCES, DE UN VISTAZO Y EN UN QUESITO.
  //
  // Las cuatro fichas de colores -"65 verde · 22 ámbar · 22 rojo · 11 sin
  // decisión"- eran cuatro números que hay que comparar mentalmente. Un quesito
  // los compara solo, que es literalmente lo que se pidió: «quesitos, donde de
  // un vistazo vea lo que pasa».
  //
  // El trozo de "sin decisión" ENTRA en el quesito, y por eso el total es la
  // ventana entera y no `g.n`. Sacarlo dejaría un gráfico donde el motor decide
  // todos los días; los once días que no decidió son parte de lo que hay que
  // auditar, igual que los huecos con borde del calendario. La línea de
  // porcentajes, que va sobre `g.n` y no sobre la ventana, se queda en el
  // detalle con su denominador pegado -sin él, "59,6 % verde" se lee sobre 120
  // días y son 109-.
  const quesitoLuces = quesito({
    trozos: [
      // Los colores son los de `COLOR_LUZ`, los mismos que pinta el calendario
      // de debajo. Un verde distinto en el quesito y en el calendario de la
      // misma pantalla haría dudar de si son el mismo verde.
      { etiqueta: nombre("green"), n: g.green, color: COLOR_LUZ.green },
      { etiqueta: nombre("amber"), n: g.amber, color: COLOR_LUZ.amber },
      { etiqueta: nombre("red"), n: g.red, color: COLOR_LUZ.red },
      { etiqueta: "sin decisión", n: g.sin_decision, color: TENUE },
    ],
    total: g.n + g.sin_decision,
    unidadTotal: plural(g.n + g.sin_decision, "día", "días"),
  });

  const detalleLuces =
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
    `<div class="desliza">${barrasSemanales(d.distribucion.por_semana, d.nombres_luz)}</div>` +
    `<p class="ficha">Una barra por semana, sobre los siete días enteros.</p>`;

  partes.push(quesitoLuces
    ? bloqueDeVistazo(
        "El color de tus días", quesitoLuces,
        `De ${cuenta(g.n + g.sin_decision, "día", "días")}, ${entero(g.green)} ` +
        `${plural(g.green, "salió", "salieron")} ${nombre("green")}, ` +
        `${entero(g.amber)} ${nombre("amber")} y ${entero(g.red)} ${nombre("red")}` +
        `${g.sin_decision
          ? `; ${entero(g.sin_decision)} ${plural(g.sin_decision, "se quedó", "se quedaron")} sin decidir`
          : ""}.`,
        detalleLuces,
      )
    : `<section class="bloque-vistazo"><h2>El color de tus días</h2>` +
      detalleLuces + `</section>`);

  // EL CALENDARIO SE QUEDA FUERA, Y NO POR COSTUMBRE. El quesito dice cuántos
  // días de cada color y el calendario dice CUÁNDO, que es otra pregunta: una
  // racha de cuatro rojos seguidos y cuatro rojos repartidos por el trimestre
  // dan el mismo quesito y no significan lo mismo. Y se entiende sin leer -es la
  // regla 4-: cuadros de colores en fila, uno por día.
  partes.push(bloqueDeVistazo(
    "Cuándo salió cada color",
    `<div class="desliza">${calendario(d.dias)}</div>`,
    "Un cuadro por día; los huecos con borde son los días que el motor no pudo decidir.",
    null,
  ));

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

  const fichasRegla = reglas.map((r) => (
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
  )).join("");

  // QUÉ REGLA MANDÓ, EN BARRAS. Es la segunda mitad de la pregunta del
  // encabezado -"¿y qué regla lo decide?"- y hasta ahora se contestaba leyendo
  // diez fichas seguidas, cada una con cuatro contadores en una línea.
  //
  // La barra es `veces_determinante` -los días en que ESA regla puso el color-
  // y no `veces_disparada`, que cuenta también los días en que saltó y mandó
  // otra más grave. La pregunta es quién decide, y quien decide es una sola por
  // día. Las que están a cero se pintan igual, con su barra en nada: una regla
  // declarada que no manda nunca es el hallazgo de esta vista, no un hueco.
  //
  // EL GRÁFICO VA DE MAYOR A MENOR Y LAS FICHAS NO, y la diferencia es a
  // propósito. Las fichas contestan "¿qué regla tengo que mirar?" y por eso
  // abren con las que nunca se evaluaron. El gráfico contesta otra cosa -"¿quién
  // manda aquí?"- y con el orden de las fichas abría con ocho renglones a cero
  // y escondía la única barra en el noveno: de un vistazo parecía un gráfico
  // roto. De mayor a menor se ve en el primer renglón que manda una sola y que
  // detrás no hay nada, que es justo lo que pasa.
  const porMando = [...reglas].sort(
    (a, b) => b.veces_determinante - a.veces_determinante,
  );
  const svgReglas = barrasTumbadas({
    valores: porMando.map((r) => r.veces_determinante),
    rotulos: porMando.map((r) => entero(r.veces_determinante)),
    etiquetas: porMando.map((r) => r.nombre),
    pie: "días en que cada regla puso el color",
  });
  const nunca = d.nunca_dispararon.length;
  partes.push(bloqueDeVistazo(
    "Qué regla decide el color",
    svgReglas,
    nunca
      ? `De ${cuenta(reglas.length, "regla", "reglas")}, ` +
        `${entero(nunca)} no ${plural(nunca, "ha disparado", "han disparado")} ` +
        `ni un día en esta ventana.`
      : `Las ${entero(reglas.length)} reglas han disparado alguna vez en esta ventana.`,
    fichasRegla,
  ));

  // LAS CUATRO LISTAS DE ABAJO SE PLIEGAN ENTERAS, sin gráfico y sin frase.
  //
  // Son registros, no medidas: cuándo se activó una regla especial, qué día
  // cambió el `config.yaml`, qué días se cerró cada puerta, cuánto progresó cada
  // ficha. Inventarles un gráfico de un vistazo sería peor que no ponerlo -un
  // dibujo de "Recalibraciones (1)" no informa de nada-, y dejarlas fuera eran
  // las mil palabras que hacían de esta vista un informe.
  //
  // La única que sí tiene una pregunta detrás es la de las puertas, y esa lleva
  // su gráfico justo debajo con los tres números que manda el servidor.
  const registro = [];
  registro.push(seccionLista(
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
  registro.push(seccionLista(
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

  const listaPuertas = seccionLista(
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
  );

  // CUANDO EL MOTOR FRENA, ¿QUÉ FRENA? Tres barras y se acabó. Los tres números
  // vienen de `resumen_puertas` y NO suman los días de la lista: un mismo día
  // puede llevar dos puertas cerradas, así que la frase dice sobre cuántos días
  // van en vez de dejar que se sumen a ojo.
  const rp = d.resumen_puertas;
  const puertas = [
    ["subir la carga", rp.carga],
    ["añadir una serie", rp.series],
    ["sumar repeticiones", rp.reps],
  ];
  const svgPuertas = barrasTumbadas({
    valores: puertas.map((p) => p[1]),
    rotulos: puertas.map((p) => entero(p[1])),
    etiquetas: ["No subir la carga", "No añadir serie", "No sumar repeticiones"],
    pie: `días con esa puerta cerrada, de ${entero(rp.dias)}`,
    positivoEsBueno: false,
  });
  // La frase nombra la puerta MÁS cerrada. No es una cuenta: es escoger el
  // máximo de tres cifras que ya vienen hechas, igual que la frase de las piezas
  // de percepción escoge la más alta de las seis medias.
  let masCerrada = puertas[0];
  for (const p of puertas) if (p[1] > masCerrada[1]) masCerrada = p;
  partes.push(bloqueDeVistazo(
    "Cuándo el motor te frenó",
    svgPuertas,
    rp.dias
      ? `En ${cuenta(rp.dias, "día", "días")} el motor cerró alguna puerta; la ` +
        `que más, la de ${masCerrada[0]}, ${cuenta(masCerrada[1], "vez", "veces")}.`
      : `El motor no cerró ninguna puerta en esta ventana.`,
    listaPuertas,
  ));

  registro.push(seccionLista(
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

  partes.push(plegable("El registro entero, tal cual", registro.join("")));

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

  partes.push(bloqueComoSalieron(d));

  // EL MENSAJE DE TELEGRAM, PLEGADO. No es una decisión de espacio: es que ese
  // texto se escribió para leerse en el móvil una mañana concreta y va entero,
  // con sus dos percentiles dentro. Traerlo tal cual a la vista principal es
  // meter cuatro líneas de prosa entre el quesito y la última sesión, que es la
  // regla 4 al revés. Va literal y sin tocar -es un registro de lo que se
  // mandó, no un resumen- pero detrás del triángulo.
  if (d.mensaje) {
    partes.push(plegable(
      "Lo último que se te mandó", `<pre>${escapar(d.mensaje)}</pre>`,
    ));
  }

  if (d.ultima) partes.push(tarjetaSesion(d.ultima, "La última sesión juzgada"));

  partes.push(pintarCobertura(null, d.ventana,
    "trabaja sobre las sesiones ya cruzadas, y cada una lleva dentro de qué " +
    "pudo juzgarse"));

  partes.push(bloqueLasPiezas(d.componentes));

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

/* EL REPARTO DE LA VENTANA, EN UN QUESITO. Las tres cosas que pueden pasar.
 *
 * Esta vista tenía el número de las disociaciones en una tarjeta, el de la
 * dirección contraria en otra más abajo y las alineadas escondidas en una línea
 * de ficha, de modo que para saber si «5 de 40» era mucho o poco había que
 * juntar tres sitios con la cabeza. Son las tres ramas del MISMO reparto y la
 * pregunta de la vista es cuál pesa más: eso es exactamente un quesito.
 *
 * LOS TRES TROZOS SUMAN EL TOTAL Y NO SE SUMAN AQUÍ. `contador.de` son las
 * sesiones juzgadas y el servidor las parte en `contador.veces`, `contraria.veces`
 * y `alineadas` (rendimiento.py: `alineadas = juzgadas - peores - mejores`). El
 * quesito recibe los tres números y el total ya hechos; si algún día el backend
 * cambiara el criterio, el dibujo cambiaría con él en vez de quedarse pintando
 * una resta vieja.
 *
 * Y si no hay denominador NO se dibuja un quesito vacío: se enseña el motivo. Un
 * anillo de cero grados y un anillo con todo alineado se parecerían demasiado, y
 * son «aún no se puede contar» frente a «la mañana acierta siempre».
 */
function bloqueComoSalieron(d) {
  const c = d.contador, k = d.contraria;

  const detalle =
    `<p class="ficha">${entero(c.total_sesiones)} sesiones registradas · ` +
    `${entero(c.sin_juicio)} sin poder juzgar · ${entero(d.alineadas)} alineadas</p>` +
    `<p class="ficha">${escapar(c.nota)}</p>` +
    (k.de
      ? `<p class="ficha">la otra dirección: ${entero(k.veces)} de ${entero(k.de)}` +
        `${k.pct !== null && k.pct !== undefined ? ` · ${pct(k.pct)}` : ""} — ` +
        `${escapar(k.que_es)}</p>`
      : bloqueNa(k.na)) +
    `<table class="tabla"><thead><tr><th>Tipo</th><th>Sesiones</th>` +
    `<th>Juzgadas</th><th>Veces</th></tr></thead><tbody>` +
    Object.entries(d.por_tipo).map(([tipo, v]) => (
      `<tr><td>${tipo === "strength" ? "Fuerza" : "Bici"}</td>` +
      `<td>${entero(v.sesiones)}</td><td>${entero(v.juzgadas)}</td>` +
      `<td>${entero(v.veces)}</td></tr>`
    )).join("") +
    `</tbody></table>` +
    (Object.keys(c.motivos || {}).length
      ? `<h3>Por qué no se pudieron juzgar</h3><ul class="motivos">` +
        Object.entries(c.motivos).map(([m, n]) => (
          `<li><b>${entero(n)}</b> — ${escapar(m)}</li>`
        )).join("") + `</ul>`
      : "");

  const svg = quesito({
    trozos: [
      {
        etiqueta: "te veías peor", sub: "y estuviste mejor",
        n: c.veces, color: AZUL,
      },
      {
        etiqueta: "te veías mejor", sub: "y estuviste peor",
        n: k.veces, color: NARANJA,
      },
      {
        etiqueta: "coincidió", sub: "sin hueco claro",
        n: d.alineadas, color: TENUE,
      },
    ],
    total: c.de,
    unidadTotal: plural(c.de || 0, "sesión juzgada", "sesiones juzgadas"),
  });

  if (!svg) {
    return (
      `<article class="tarjeta"><h2>Cómo salieron las sesiones</h2>` +
      bloqueNa(c.na) + plegable("ver detalle", detalle) + `</article>`
    );
  }

  return bloqueDeVistazo(
    "Cómo salieron las sesiones",
    svg,
    `De ${cuenta(c.de, "sesión juzgada", "sesiones juzgadas")}, en ` +
    `${entero(c.veces)} te veías peor de lo que luego estuviste` +
    `${c.pct !== null && c.pct !== undefined ? ` (${pct(c.pct)})` : ""}.`,
    detalle,
  );
}

/* LAS PIEZAS DEL ÍNDICE, EN BARRAS. Para ver cuál lo sostiene y cuál está plana.
 *
 * Era una lista de seis `<label>` con su número a la derecha, que es una tabla
 * disfrazada: para saber cuál tira del índice había que comparar seis cifras
 * leyéndolas. Tumbadas, la más larga se ve antes de leer ninguna.
 *
 * Van TUMBADAS y no verticales por la regla 5: los rótulos que manda el servidor
 * son «Corazón, frente a tus salidas de siempre», y seis de esos debajo de seis
 * barras de cincuenta píxeles en un móvil en vertical no se leen sin girar la
 * cabeza.
 *
 * Una pieza sin media deja su rótulo puesto y no dibuja barra -no dibuja un
 * cero-, y su motivo va en el detalle. Un cero ahí diría «esa pieza salió
 * fatal» cuando lo que pasa es que ninguna sesión de la ventana la trae.
 */
function bloqueLasPiezas(componentes) {
  const entradas = Object.entries(componentes || {});
  if (!entradas.length) return "";

  const svg = barrasTumbadas({
    valores: entradas.map(([, v]) => v.media),
    rotulos: entradas.map(([, v]) => num(v.media, 0)),
    // La etiqueta la manda el servidor. Si algún día faltara se ve la clave, que
    // es fea pero cierta; lo que no se hace es tener aquí una segunda lista de
    // nombres que se quede vieja sin que nada lo diga.
    etiquetas: entradas.map(([nombre, v]) => (
      (v.etiqueta || nombre) + (v.entra_en_el_indice ? "" : " (fuera del índice)")
    )),
    pie: "media de cada pieza en la ventana, de 0 a 100",
  });

  const detalle = entradas.map(([nombre, v]) => (
    `<p class="ficha"><b>${escapar(v.etiqueta || nombre)}</b> — ` +
    (v.media === null || v.media === undefined
      ? escapar(v.na)
      // Y aquí `n` son SESIONES. Tercer significado de la misma letra en la
      // misma pantalla; por eso ninguna de las tres la lleva ya.
      : `${num(v.media, 1)} de media de ${cuenta(v.n, "sesión", "sesiones")}`) +
    `${v.entra_en_el_indice ? "" : " · no entra en el índice"}</p>`
  )).join("");

  if (!svg) {
    return (
      `<article class="tarjeta"><h2>Las piezas del índice</h2>` +
      bloqueNa("ninguna pieza tiene media en esta ventana") +
      plegable("ver detalle", detalle) + `</article>`
    );
  }

  const conMedia = entradas.filter(
    ([, v]) => v.media !== null && v.media !== undefined,
  );
  // La frase nombra la pieza MÁS ALTA, que es la que sostiene el índice. No es
  // un cálculo: es elegir el máximo de una lista que ya viene hecha.
  let alta = conMedia[0];
  for (const e of conMedia) if (e[1].media > alta[1].media) alta = e;

  return bloqueDeVistazo(
    "Las piezas del índice",
    svg,
    `La que más sostiene el índice es «${alta[1].etiqueta || alta[0]}», ` +
    `con ${num(alta[1].media, 0)} de media.`,
    detalle,
  );
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
    // LA FRASE DE FUERA NO DICE «PERCENTIL» Y NO ES UN EUFEMISMO. Decía «hiciste
    // 31 puntos de percentil por encima de lo que esperabas», que para saber si
    // 31 es mucho obliga a saber qué es un percentil. Lo que la tarjeta tiene que
    // contestar de un vistazo es en qué dirección falló la mañana, y si falló por
    // mucho; las dos cosas vienen del servidor -el signo del `gap` y el booleano
    // `disociacion`, que es el que lleva el umbral- y ninguna se decide aquí.
    // Los 31 puntos siguen existiendo, en el detalle, con su percentil al lado.
    (s.gap !== null && s.gap !== undefined
      ? `<p class="cifra">Estuviste ` +
        `<b>${Number(s.gap) >= 0 ? "mejor" : "peor"}</b> de lo que te veías` +
        `${s.disociacion ? ", y por mucho" : ""}</p>`
      : bloqueNa(s.na)) +
    plegable("ver detalle",
      `<p class="ficha">${num(s.gap, 0)} puntos de percentil por ` +
      `${Number(s.gap) >= 0 ? "encima" : "debajo"} de lo que esperabas</p>` +
      `<p class="ficha">esperabas ${num(s.percepcion, 1)} (percentil ` +
      `${num(s.percepcion_pct, 0)}) · hiciste ${num(s.rendimiento, 1)} (percentil ` +
      `${num(s.rendimiento_pct, 0)}) · ${entero(s.n_base)} sesiones detrás</p>` +
      (piezas ? `<p class="ficha">${piezas}</p>` : "")) +
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
    bloqueCuantoBaja(d),
    bloqueCuantoTarda(d),
    bloqueTuHrv(d),
    bloqueComoSonTusSalidas(d),
  ];

  $("vista").innerHTML = partes.join("");
}

/* EL ESCALÓN. Cuatro barras y se ve solo.
 *
 * `tramos` son cuartiles por carga y cada uno trae su media y su `frase` ya
 * escrita. La barra usa `media` para la altura y `frase` para el número que se
 * lee: `rotuloDeFrase` saca «−7,0» de «baja 7,0 ms» en vez de volver a
 * redondear la media, porque el servidor escribe sus frases sobre el número sin
 * redondear -«baja 0,2 ms» sobre una media de −0,15- y cualquier redondeo hecho
 * aquí acertaría en unos tramos y fallaría en otros. Entonces la cifra de
 * encima de la barra y la de la tabla del detalle dirían cosas distintas del
 * mismo dato.
 */
function rotuloDeFrase(frase) {
  const partes = String(frase).trim().split(/\s+/);
  return (partes[0] === "baja" ? "−" : "+") + partes[1];
}

/* Las etiquetas del eje de abajo, en dos renglones.
 *
 * «de 5 a 43 de carga» es la etiqueta del servidor y es perfecta para la tabla
 * del detalle; debajo de una barra de 70 píxeles de ancho se sale por los dos
 * lados o hay que encogerla hasta que no se lea. Se parte en dos líneas cortas
 * -«5 a» / «43»- y la palabra «carga» se dice UNA vez, en el pie del gráfico.
 */
function etiquetaTramo(t) {
  const a = num(t.desde_carga, 0);
  return t.ultimo ? ["más de", a] : [`${a} a`, num(t.hasta_carga, 0)];
}

function bloqueCuantoBaja(d) {
  const u = d.umbral;
  const f = u.frontera;
  if (u.na || !u.tramos || !u.tramos.length) {
    return bloqueDeVistazo("Cuánto te baja la HRV según la salida", "",
      "", bloqueNa(u.na));
  }

  const g = barrasDeVistazo({
    valores: u.tramos.map((t) => t.media),
    rotulos: u.tramos.map((t) => (t.media === null ? "" : rotuloDeFrase(t.frase))),
    etiquetas: u.tramos.map(etiquetaTramo),
    pie: "carga de la salida",
    unidad: "ms",
  });

  const duro = u.tramos[u.tramos.length - 1];
  const frase = duro.media === null
    ? ""
    : `Solo las salidas de más de ${num(duro.desde_carga, 0)} de carga se ` +
      `notan: la HRV ${duro.frase} a la mañana siguiente.`;

  return bloqueDeVistazo("Cuánto te baja la HRV según la salida", g, frase,
    detalleDelEscalon(d, u, f));
}

/* TODO LO QUE ERA LA VISTA PRINCIPAL, AHORA PLEGADO.
 *
 * Aquí dentro está lo que antes ocupaba media pantalla: la tabla de tramos, los
 * nueve cortes que perdieron, la p corregida, la correlación continua. No se ha
 * borrado ni una cifra -la regla era esconderlas, no tirarlas, y el detalle es
 * justo lo que se abre cuando la frase de arriba no basta-, pero ya no es lo
 * primero que se ve al abrir el panel en el móvil.
 */
function detalleDelEscalon(d, u, f) {
  if (f.na) return bloqueNa(f.na);
  const c = f.corte;
  return (
    `<p class="lectura">${escapar(f.lectura)}</p>` +
    `<p class="explica">Tus salidas repartidas en cuatro grupos del mismo ` +
    `tamaño por carga, y lo que hizo la HRV la mañana de después de cada uno. ` +
    `Esta tabla se parte en cuatro y el corte de abajo se busca en diez: son ` +
    `dos reglas distintas y no tienen por qué caer en el mismo sitio.</p>` +
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
    `<h3>El corte, y los que perdieron</h3>` +
    `<p class="ficha">El corte cae exactamente en ${num(f.carga, 1)} de carga · ` +
    `${cuenta(c.n_debajo, "salida", "salidas")} por debajo · ` +
    `${entero(c.n_encima)} por encima</p>` +
    `<p class="medias">Por debajo ${escapar(c.debajo.frase || "—")}, por encima ` +
    `${escapar(c.encima.frase || "—")}. Diferencia <b>${num(c.diferencia)}</b> ms` +
    `${c.p_corregida !== null && c.p_corregida !== undefined
      ? ` · p corregida ${num(c.p_corregida, 3)}` : ""}` +
    `${c.significativa === true ? " · aguanta la corrección" : ""}` +
    `${c.significativa === false ? " · no aguanta la corrección" : ""}` +
    ` sobre ${entero(f.miradas)} miradas.</p>` +
    ficha(c) +
    detalleContinua(f) +
    tablaCandidatos(f) +
    `<p class="aviso tenue">${escapar(d.convenio)}</p>`
  );
}

/* ¿ESCALÓN O CUESTA? El control que decide si el número grande significa algo.
 *
 * Estaba arriba y sin plegar, y con un comentario que decía por qué: «lo que
 * distingue un umbral de una raya arbitraria es que la relación suave no
 * explique ya lo mismo». Sigue siendo verdad y el bloque sigue entero. Lo que
 * cambia es dónde: una barra de −1 a 1 con un «r = −0,38» al lado es exactamente
 * la pantalla para estudiar que se pidió quitar de delante.
 *
 * Lo que la vista principal dice ahora sobre esto es la frase de arriba, que la
 * escribe el servidor en `escalon` sabiendo las dos correcciones. Aquí abajo
 * está el número por si la frase no basta, que es para lo que existe el pliegue.
 */
function detalleContinua(f) {
  const k = f.continua;
  if (!k) return "";
  return (
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
    (f.escalon ? `<p class="lectura">${escapar(f.escalon)}</p>` : "")
  );
}

/* Los cortes que PERDIERON, con sus números.
 *
 * Enseñar solo el ganador de una búsqueda entre nueve es la forma más limpia de
 * que un empate parezca un hallazgo: si el de 116 y el de 147 separan casi lo
 * mismo, eso se ve aquí y en ningún otro sitio. */
function tablaCandidatos(f) {
  const lista = f.candidatos || [];
  if (!lista.length) return "";
  return (
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
    `</tbody></table>`
  );
}

/* CUÁNTO TARDA EN VOLVER. Día +1, +2, +3, +4.
 *
 * La primera curva es la de las salidas duras -las que pasan el corte- y es la
 * que contesta la pregunta. La segunda, con todas, va al detalle: es la misma
 * cuenta sin el filtro, y sirve para ver que el efecto no sale igual mirando
 * cualquier salida.
 */
function bloqueCuantoTarda(d) {
  const curvas = (d.recuperacion && d.recuperacion.curvas) || [];
  const dura = curvas[0];
  if (!dura || dura.na) {
    return bloqueDeVistazo("Cuánto tarda en volver", "", "",
      bloqueNa((dura && dura.na) || "no hay curva de recuperación"));
  }
  const dias = dura.por_dia || [];

  const g = barrasDeVistazo({
    valores: dias.map((p) => p.media),
    rotulos: dias.map((p) => (p.media === null ? "" : rotuloDeFrase(p.frase))),
    etiquetas: dias.map((p) => [`+${entero(p.dia)}`]),
    pie: "días después de la salida",
    unidad: "ms",
  });

  const primero = dias[0];
  const frase =
    primero && primero.media !== null && dura.vuelve_el_dia !== null &&
    dura.vuelve_el_dia !== undefined
      ? `Una noche: al día siguiente la HRV ${primero.frase} y el día ` +
        `+${entero(dura.vuelve_el_dia)} ya ha vuelto.`
      : (dura.lectura || "");

  const detalle =
    `<p class="lectura">${escapar(dura.lectura || "")}</p>` +
    (dura.aviso ? `<p class="aviso">${escapar(dura.aviso)}</p>` : "") +
    `<p class="ficha">${cuenta(dura.n_salidas, "salida aislada", "salidas aisladas")}` +
    `${dura.desde_carga !== null && dura.desde_carga !== undefined
      ? ` de ${num(dura.desde_carga, 0)} de carga para arriba` : ""}</p>` +
    curvas.map((c) => (
      `<h3>${escapar(c.titulo)}</h3>` +
      (c.na ? bloqueNa(c.na) : tablaPorDia(c.por_dia || []))
    )).join("") +
    `<p class="explica">${escapar(d.recuperacion.nota)}</p>` +
    `<p class="aviso tenue">${escapar(d.sin_p)}</p>`;

  return bloqueDeVistazo("Cuánto tarda en volver", g, frase, detalle);
}

function tablaPorDia(dias) {
  return (
    `<table class="tabla"><thead><tr><th>Día</th><th>HRV</th>` +
    `<th>Salidas</th><th>Mitad central</th></tr></thead><tbody>` +
    dias.map((p) => (
      `<tr><td>+${entero(p.dia)}</td>` +
      (p.media === null || p.media === undefined
        ? `<td class="motivo" colspan="3">${escapar(p.na || "")}</td>`
        : `<td>${num(p.media)} ms</td><td>${entero(p.n)}</td>` +
          `<td>${num(p.p25)} a ${num(p.p75)}</td>`) +
      `</tr>`
    )).join("") +
    `</tbody></table>`
  );
}

/* TU HRV, LA LÍNEA Y TU MEDIA. Y nada más encima.
 *
 * Lo que se quita respecto de `hrvConSalidas`, que sigue existiendo y sigue
 * siendo correcto: la banda de la mitad central, los palos de las salidas y la
 * raya del corte. Los tres estaban bien calculados y los tres obligaban a un
 * pie de foto de cuatro renglones explicando qué es cada trazo. Ese pie es el
 * párrafo de la regla 4 escrito en letra pequeña.
 *
 * La banda no se pierde: está en el detalle, en palabras, que es donde se puede
 * decir «la mitad de tus noches caen entre 43 y 54» en vez de dibujar una
 * franja gris que hay que descifrar.
 */
function bloqueTuHrv(d) {
  const g = d.grafica;
  if (g.na) {
    return bloqueDeVistazo("Tu HRV en esta ventana", "", "", bloqueNa(g.na));
  }

  const dibujo = lineaConMedia({
    // La línea es la media móvil, no las medidas sueltas: una noche de HRV se
    // mueve lo que le da la gana y el dibujo en crudo es una sierra donde no se
    // ve ninguna tendencia. Las medidas sueltas siguen en `rango`, en el detalle.
    puntos: (g.puntos || []).map((p) => ({ fecha: p.fecha, valor: p.suave })),
    media: g.media,
    rotuloMedia: `tu media, ${num(g.media, 0)} ms`,
    unidad: "ms",
  });

  const frase = g.media === null || g.ahora === null || g.ahora === undefined
    ? ""
    : `Tu media son ${num(g.media, 0)} ms y ahora andas por ${num(g.ahora, 0)}.`;

  const detalle =
    `<p class="explica">La línea es la media de ` +
    `${cuenta(g.suavizado, "día", "días")} sobre ` +
    `${cuenta(g.n, "noche medida", "noches medidas")}, del ` +
    `${fechaCorta(d.ventana.desde)} al ${fechaCorta(d.ventana.hasta)}.` +
    `${g.rango ? ` La medida de cada noche suelta va de ${num(g.rango[0], 0)} a ` +
      `${num(g.rango[1], 0)} ms.` : ""}</p>` +
    (g.banda
      ? `<p class="ficha">Tu mitad central está entre ${num(g.banda.desde, 0)} y ` +
        `${num(g.banda.hasta, 0)} ms: la mitad de tus noches caen ahí dentro.</p>`
      : "") +
    `<p class="aviso tenue">${escapar(d.convenio)}</p>`;

  return bloqueDeVistazo("Tu HRV en esta ventana", dibujo, frase, detalle);
}

/* CÓMO SON TUS SALIDAS. El quesito, con el corte de arriba como criterio.
 *
 * Las dos mitades del anillo son las mismas dos mitades con las que se calculó
 * el escalón del primer bloque, y eso importa: sin el reparto, «las de más de
 * 176 se notan» se lee sin saber si eso son dos salidas o la mitad de ellas.
 */
function bloqueComoSonTusSalidas(d) {
  const f = d.umbral.frontera;
  if (f.na || !f.corte) {
    return bloqueDeVistazo("Cómo son tus salidas", "", "",
      `<p class="ficha">${escapar(d.salidas.resumen)}</p>`);
  }
  const c = f.corte;
  const total = c.n_encima + c.n_debajo;

  const g = quesito({
    trozos: [
      { etiqueta: "duras", sub: `más de ${num(f.carga, 0)} de carga`,
        n: c.n_encima, color: NARANJA },
      { etiqueta: "suaves", sub: `hasta ${num(f.carga, 0)}`,
        n: c.n_debajo, color: AZUL },
    ],
    total,
    unidadTotal: "salidas",
  });

  const frase = c.de_cada_diez === null || c.de_cada_diez === undefined
    ? ""
    : `De cada 10 salidas, ${entero(c.de_cada_diez)} son de las que te ` +
      `cuestan una noche.`;

  const detalle =
    `<p class="ficha">${escapar(d.salidas.resumen)}</p>` +
    `<p class="explica">De las ${entero(d.salidas.medidas)} salidas medidas, ` +
    `${entero(d.salidas.aisladas)} están aisladas —sin otra salida en los ` +
    `${cuenta(d.recuperacion.aislamiento, "día", "días")} de antes ni de ` +
    `después— y son las únicas que entran en la curva de recuperación.</p>` +
    // El dibujo antiguo, con los palos de las salidas y la raya del corte. No
    // se tira: es lo único de esta pantalla que enseña CUÁNDO pasó cada cosa, y
    // eso no lo cuenta ni el escalón ni el anillo. Pero necesita su leyenda de
    // cuatro renglones para leerse, así que vive donde una leyenda no molesta.
    hrvConSalidas(d.grafica) +
    pieGrafica(d.grafica);

  return bloqueDeVistazo("Cómo son tus salidas", g, frase, detalle);
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

// ---------------------------------------------------------------------------
// Calibración: dónde el motor y tú no estáis de acuerdo
// ---------------------------------------------------------------------------

/* LA ÚNICA VISTA QUE MIDE AL USUARIO Y AL MOTOR A LA VEZ, y por eso la única
 * donde pintar de más es mentir.
 *
 * Las otras siete miran datos: la HRV bajó tanto, la sesión salió en tal
 * percentil. Ésta mira OPINIONES -tus síes y tus noes al previsualizar- y las
 * cruza con resultados. Eso trae dos trampas propias que se resuelven aquí y no
 * en el dibujo:
 *
 *   - NINGÚN COLOR SIGNIFICA «BIEN» O «MAL». Discrepar mucho no es un defecto
 *     tuyo ni del motor; es el dato. Pintar «más dura» en rojo y «más suave» en
 *     verde metería un juicio que no hay y que nadie ha calculado. Los colores
 *     del semáforo sí se usan, y solo donde el color ES el dato -el agolpamiento
 *     por luz-, porque ese significado lo pone el servidor.
 *   - EL VEREDICTO SE CALLA Y SE DICE QUE SE CALLA. Es lo único de esta pantalla
 *     que necesita muestra. Cuando no la hay se pinta el bloque igual, con el
 *     contador de cuánto falta: un bloque escondido hasta el quinto caso se lee
 *     como una vista a medio hacer.
 *
 * Y la etiqueta del juez se pinta SIEMPRE, con casos o sin ellos, porque es lo
 * que dice cómo leer el bloque entero. Enseñarla solo cuando hay veredicto la
 * convertiría en una nota al pie del resultado en vez de en la advertencia que
 * es. */
async function pintarCalibracion(dias) {
  const d = await pedir(RUTAS.calibracion, { dias });

  const partes = [
    encabezadoVista(d.encabezado),
    // `null` a propósito, y no `d.cobertura`: esta vista NO manda cobertura, y
    // pedírsela era leer una clave que el payload no trae. Las demás miran
    // cuatro fuentes y saber de cuál falta qué es media lectura; ésta mira una
    // sola tabla, la de previsualizaciones, y su cobertura es el recuento que va
    // justo debajo. Enseñar aquí "sin datos de Garmin" sería afirmar un vacío
    // que esta pantalla no ha mirado.
    pintarCobertura(null, d.ventana,
      "mira una sola tabla, la de las previsualizaciones que has pedido tú; " +
      "cuántas hay y en cuántas dijiste algo va justo aquí debajo"),
    bloqueCuantoDiscrepas(d.cuantas),
    bloqueHaciaDonde(d.direcciones),
    bloqueDondeSeAgolpa(d.donde),
    bloqueAnulaciones(d.anulaciones),
    bloqueQuienAcerto(d.quien_acerto),
  ];

  $("vista").innerHTML = partes.join("");
}

/* Cuántas veces, sobre las opinadas.
 *
 * El anillo tiene TRES trozos y no dos, y el tercero -«sin opinar»- es el que
 * hace que el porcentaje se entienda. La cifra de arriba sale sobre las
 * opinadas; el anillo enseña cuántas quedaron fuera de ese denominador. Con dos
 * trozos, «30 %» y un anillo lleno dirían que se han mirado todas. */
function bloqueCuantoDiscrepas(c) {
  if (!c || c.na || !c.opinadas) {
    return bloqueDeVistazo("Cuántas veces no lo compartes", "", "",
      bloqueNa((c && c.na) || null));
  }

  const g = quesito({
    trozos: [
      { etiqueta: plural(c.discrepadas, "no lo comparto", "no lo comparto"),
        n: c.discrepadas, color: NARANJA },
      { etiqueta: "de acuerdo", n: c.conformes, color: AZUL },
      { etiqueta: "sin opinar", n: c.sin_opinar, color: TENUE },
    ],
    total: c.total,
    unidadTotal: plural(c.total, "previsualización", "previsualizaciones"),
  });

  const frase =
    `De las ${cuenta(c.opinadas, "vez", "veces")} que dijiste algo, no ` +
    `compartiste lo que decidía el motor en ${entero(c.discrepadas)}: ` +
    `${pct(c.pct, 1)}.`;

  return bloqueDeVistazo("Cuántas veces no lo compartes", g, frase,
    `<p class="explica">El porcentaje va sobre las ${entero(c.opinadas)} en ` +
    `las que dijiste algo, NO sobre las ${entero(c.total)} que miraste. Mirar ` +
    `una previsualización y cerrarla sin contestar no es estar de acuerdo, y ` +
    `contarlo como tal bajaría el porcentaje cuanto menos contestaras.</p>` +
    `<table class="tabla"><tbody>` +
    `<tr><td>No lo comparto</td><td>${entero(c.discrepadas)}</td></tr>` +
    `<tr><td>De acuerdo</td><td>${entero(c.conformes)}</td></tr>` +
    `<tr><td>Sin opinar</td><td>${entero(c.sin_opinar)}</td></tr>` +
    `<tr><td><b>Previsualizaciones</b></td><td><b>${entero(c.total)}</b></td></tr>` +
    `</tbody></table>`);
}

/* Hacia qué lado. Tres barras tumbadas y las forzadas en rojo aparte.
 *
 * `positivoEsBueno: false` NO está eligiendo que discrepar sea malo: es lo que
 * apaga el coloreado por signo de `barrasTumbadas`. Aquí todas las barras son
 * cuentas positivas de la misma cosa y pintarlas de dos colores según nada
 * sería el juicio que esta vista no hace.
 *
 * Las forzadas en rojo van en una línea aparte y NUNCA como cuarta barra: son
 * un subconjunto de «más dura», así que sumadas darían más que el total y
 * parecería que se ha perdido la cuenta. El servidor manda esa advertencia
 * escrita en `que_es` y se pinta tal cual. */
function bloqueHaciaDonde(dir) {
  if (!dir || dir.na || !dir.n) {
    return bloqueDeVistazo("Hacia qué lado tiras", "", "",
      bloqueNa((dir && dir.na) || null));
  }

  const g = barrasTumbadas({
    valores: dir.celdas.map((c) => c.n),
    rotulos: dir.celdas.map((c) => entero(c.n)),
    // La corta encima de la barra y la larga en la tabla del detalle: las dos
    // las manda el servidor. «Dijiste que no lo compartías, sin pedir otra
    // sesión» encima de una barra de 340 píxeles se sale por el lado derecho, y
    // un SVG no recorta: dibuja fuera del `viewBox` y desaparece.
    etiquetas: dir.celdas.map((c) => c.corta),
    pie: plural(dir.n, "desacuerdo", "desacuerdos"),
    positivoEsBueno: false,
  });

  return bloqueDeVistazo("Hacia qué lado tiras", g, dir.lectura || "",
    `<table class="tabla"><thead><tr><th>Cuando no lo compartes</th>` +
    `<th>Veces</th><th>De los desacuerdos</th></tr></thead><tbody>` +
    dir.celdas.map((c) => (
      `<tr><td>${escapar(c.etiqueta)}</td><td>${entero(c.n)}</td>` +
      `<td>${pct(c.pct, 1)}</td></tr>`
    )).join("") +
    `</tbody></table>` +
    `<p class="ficha">De las que pedían más dura, ` +
    `${cuenta(dir.forzadas_en_rojo, "fue", "fueron")} con el semáforo en rojo, ` +
    `confirmando a mano.</p>` +
    `<p class="explica">${escapar(dir.que_es)}</p>`);
}

/* Dónde se agolpa. La tabla lleva LOS DOS porcentajes y ninguno viaja solo.
 *
 * Es la única tabla de la pantalla con dos columnas de porcentaje, y la
 * explicación de por qué va encima y no en una nota al pie: leída una sola de
 * las dos columnas, esta tabla señala el sitio equivocado en los dos casos
 * normales -la regla que dispara todos los días, y la que disparó dos veces-.
 * El texto lo escribe el servidor en `que_es`. */
function bloqueDondeSeAgolpa(w) {
  if (!w || w.na || !w.por_regla.length) {
    return bloqueDeVistazo("En qué regla se concentra", "", "",
      bloqueNa((w && w.na) || null));
  }

  const g = barrasTumbadas({
    valores: w.por_regla.map((g) => g.discrepadas),
    rotulos: w.por_regla.map((g) => entero(g.discrepadas)),
    etiquetas: w.por_regla.map((g) => g.clave),
    pie: plural(w.n, "desacuerdo", "desacuerdos"),
    positivoEsBueno: false,
  });

  return bloqueDeVistazo("En qué regla se concentra", g, w.lectura || "",
    `<p class="explica">${escapar(w.que_es)}</p>` +
    `<p class="explica">Las dos columnas de porcentaje contestan preguntas ` +
    `distintas y hay que leerlas juntas. <b>Del total</b> es cuánto pesa esa ` +
    `regla dentro de todo lo que discrepas; <b>cuando aparece</b> es con qué ` +
    `frecuencia discrepas los días que esa regla decide. Una regla que dispara ` +
    `a diario se lleva la primera columna aunque sea con la que más de acuerdo ` +
    `estás.</p>` +
    tablaAgolpe("Regla que decidió el día", w.por_regla) +
    `<h3>Por color del día</h3>` +
    tablaAgolpe("Semáforo", w.por_luz, NOMBRE_LUZ_PWA));
}

/* EL CONTADOR DE ANULACIONES, por la regla que decidió el día.
 *
 * Es otra cosa que el bloque de arriba y por eso va aparte. Aquél cuenta
 * DESACUERDOS -marcar «no lo veo», que es una opinión y no cambia el entreno-;
 * éste cuenta ANULACIONES: los días en que además pediste otra sesión y el
 * sistema escribió la tuya en Hevy. Solo las segundas dejan una sesión
 * distinta de la propuesta, que es la única que después se puede juzgar.
 *
 * Y LO QUE ESTE BLOQUE DICE SOBRE TODO ES CUÁNTO FALTA. No ajusta nada, no
 * propone nada y no aprende: cuenta. El módulo que mida quién acertó se
 * escribirá cuando haya diez casos reales, y con este contador delante se
 * sabrá cuándo. Mientras tanto, la única frase honrada es «van N de 10».
 */
function bloqueAnulaciones(a) {
  if (!a || a.na || !a.por_regla.length) {
    return bloqueDeVistazo("Cuántas veces has pedido otra sesión", "", "",
      bloqueNa((a && a.na) || null));
  }

  const g = barrasTumbadas({
    valores: a.por_regla.map((r) => r.anulaciones),
    rotulos: a.por_regla.map((r) => entero(r.anulaciones)),
    etiquetas: a.por_regla.map((r) => r.clave),
    pie: plural(a.n, "anulación", "anulaciones"),
    positivoEsBueno: false,
  });

  // La frase de arriba es la de la regla con más anulaciones, que es de la
  // que antes se va a poder decir algo. Sale del servidor ya redactada: quien
  // sabe cuántas van y cuántas faltan es quien las contó.
  return bloqueDeVistazo(
    "Cuántas veces has pedido otra sesión", g, a.por_regla[0].lectura || "",
    `<p class="explica">${escapar(a.que_es)}</p>` +
    `<p class="explica">Con ${escapar(entero(a.minimo))} anulaciones de una ` +
    `misma regla se podrá mirar si su umbral está donde debería, y el sistema ` +
    `lo propondrá para que lo apruebes o no. <b>No se ajusta solo.</b> Hasta ` +
    `entonces esto solo cuenta, y dice cuánto falta.</p>` +
    tablaAnulaciones(a.por_regla));
}

function tablaAnulaciones(grupos) {
  return (
    `<table class="tabla"><thead><tr><th>Regla que decidió el día</th>` +
    `<th>Anulaciones</th><th>Faltan</th><th>Forzadas en rojo</th>` +
    `<th>Qué pediste</th></tr></thead><tbody>` +
    grupos.map((r) => {
      const pedidas = Object.entries(r.pedidas || {})
        .sort((a, b) => b[1] - a[1])
        .map(([k, n]) => `${escapar(NOMBRE_SESION_PWA[k] || k)} ×${entero(n)}`)
        .join(", ");
      return (
        `<tr><td>${escapar(r.clave)}</td>` +
        `<td>${entero(r.anulaciones)}</td>` +
        `<td>${r.faltan ? entero(r.faltan) : "—"}</td>` +
        `<td>${r.forzadas_en_rojo ? entero(r.forzadas_en_rojo) : "—"}</td>` +
        `<td>${pedidas}</td></tr>`
      );
    }).join("") +
    `</tbody></table>`
  );
}

/* Los tres tipos de sesión en castellano. Diccionario de clave a palabra, como
 * `NOMBRE_LUZ_PWA`: aquí no se decide nada, se traduce. Con el valor crudo de
 * respaldo, que se lee peor y es mejor que un hueco. */
const NOMBRE_SESION_PWA = {
  full: "completa",
  reduced: "reducida",
  recovery: "recuperación",
};

function tablaAgolpe(cabecera, grupos, nombres = null) {
  return (
    `<table class="tabla"><thead><tr><th>${escapar(cabecera)}</th>` +
    `<th>Discrepadas</th><th>Opinadas</th><th>Del total</th>` +
    `<th>Cuando aparece</th></tr></thead><tbody>` +
    grupos.map((g) => (
      `<tr><td>${escapar((nombres && nombres[g.clave]) || g.clave)}</td>` +
      `<td>${entero(g.discrepadas)}</td><td>${entero(g.opinadas)}</td>` +
      `<td>${pct(g.pct_de_los_desacuerdos, 1)}</td>` +
      `<td>${pct(g.pct_cuando_aparece, 1)}</td></tr>`
    )).join("") +
    `</tbody></table>`
  );
}

/* Quién acertó. El bloque que se calla, y que dice que se calla.
 *
 * Tres cosas se pintan pase lo que pase: la etiqueta del juez, el contador de
 * cuánto falta y la lista de casos con su motivo. Lo único que desaparece
 * cuando no hay muestra es la FRASE que compara, y en su sitio va el `na` del
 * servidor, que dice cuántos van de los que hacen falta.
 *
 * La etiqueta va ARRIBA, antes de las medianas. Debajo se leería como una
 * matización de un resultado ya leído; encima es la instrucción de cómo leerlo. */
function bloqueQuienAcerto(q) {
  if (!q) return "";

  const medianas =
    q.mediana_discrepando === null || q.mediana_discrepando === undefined ||
    q.mediana_el_resto === null || q.mediana_el_resto === undefined
      ? ""
      : `<p class="medias">Llevándole la contraria, percentil ` +
        `<b>${num(q.mediana_discrepando, 1)}</b> en ` +
        `${cuenta(q.n, "sesión", "sesiones")}. El resto de los días medidos, ` +
        `<b>${num(q.mediana_el_resto, 1)}</b> en ` +
        `${cuenta(q.n_el_resto, "sesión", "sesiones")}.</p>`;

  const detalle =
    `<p class="aviso">${escapar(q.etiqueta_del_juez)}</p>` +
    `<p class="ficha">${cuenta(q.n, "caso juzgable", "casos juzgables")} de ` +
    `los ${entero(q.hacen_falta)} que hacen falta para comparar.</p>` +
    medianas +
    listaCasos(q.casos);

  return bloqueDeVistazo("Cómo salió cuando le llevaste la contraria", "",
    q.veredicto || "", q.na ? bloqueNa(q.na) + detalle : detalle);
}

/* Cada desacuerdo con su motivo de por qué no juzga, cuando no juzga.
 *
 * Los cuatro motivos -no se envió, no pediste otra cosa, se ejecutó otra, sin
 * resultado medido- vienen escritos del servidor y cada uno señala algo
 * distinto que arreglar. Resumirlos aquí a «no evaluable» juntaría cuatro
 * problemas en una palabra. */
function listaCasos(casos) {
  if (!casos || !casos.length) {
    return `<p class="ficha">Ni un desacuerdo en la ventana.</p>`;
  }
  return (
    `<h3>Los desacuerdos, uno a uno</h3>` +
    `<table class="tabla"><thead><tr><th>Día</th><th>Propuesta</th>` +
    `<th>Pediste</th><th>Resultado</th></tr></thead><tbody>` +
    casos.map((c) => (
      `<tr><td>${escapar(fechaCorta(c.fecha))}` +
      `${c.forzada_en_rojo ? ` <span class="sub">— forzada en rojo</span>` : ""}` +
      `</td><td>${escapar(c.propuesta || "—")}</td>` +
      `<td>${escapar(c.pediste || "—")}</td>` +
      (c.na
        ? `<td class="motivo">${escapar(c.na)}</td>`
        : `<td>percentil ${num(c.rendimiento_pct, 1)}</td>`) +
      `</tr>`
    )).join("") +
    `</tbody></table>`
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
