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
  concordancia: "/api/metrics/concordancia",
  desfase: "/api/metrics/desfase",
  impacto: "/api/metrics/impacto",
  ranking: "/api/metrics/ranking-ejercicios",
  auditoria: "/api/metrics/auditoria",
  percepcion: "/api/metrics/percepcion",
};

const VISTAS = {
  concordancia: {
    titulo: "Concordancia",
    subtitulo: "Lo que notas frente a lo que mide el reloj, el mismo día",
    pintar: pintarConcordancia,
  },
  desfase: {
    titulo: "Desfase",
    subtitulo: "La misma pregunta, corriendo la ventana de −3 a +3 días",
    pintar: pintarDesfase,
  },
  impacto: {
    titulo: "Impacto",
    subtitulo: "Qué le hace al cuerpo cada cosa, uno, dos y tres días después",
    pintar: pintarImpacto,
  },
  auditoria: {
    titulo: "Auditoría",
    subtitulo: "El motor auditándose: qué decidió, con qué regla y con qué datos",
    pintar: pintarAuditoria,
  },
  percepcion: {
    titulo: "Percepción",
    subtitulo: "Lo que la mañana prometía frente a lo que de verdad salió",
    pintar: pintarPercepcion,
  },
};

const POR_DEFECTO = "concordancia";
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
// Vista 1: concordancia
// ---------------------------------------------------------------------------

async function pintarConcordancia(dias) {
  const d = await pedir(RUTAS.concordancia, { dias });
  const partes = [pintarCobertura(d.cobertura, d.ventana)];

  partes.push(
    `<p class="explica">Cada pareja junta algo que contestas por la mañana con ` +
    `algo que el reloj mide solo. La barra va de −1 a 1 con el cero marcado, y ` +
    `<b>el signo que se espera</b> está escrito debajo: lo interesante no es que ` +
    `la correlación sea alta, es que vaya en la dirección que debería.</p>`,
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

  // Las señales sueltas, plegadas. Cada una en su propio percentil histórico,
  // que es lo único que permite dibujar un 1-5 y una HRV en milisegundos en la
  // misma escala sin que una de las dos sea una raya pegada al suelo.
  const series = d.series.map((s) => (
    `<div class="serie">` +
    `<div class="linea"><label>${escapar(s.etiqueta)}</label>` +
    `<output class="valor">n = ${entero(s.n)}</output></div>` +
    (s.n
      ? serieTemporal(s.puntos, [0, 100], "escala")
      : bloqueNa(`no hay ni un día con ${escapar(s.etiqueta.toLowerCase())} en esta ventana`)) +
    `<p class="ficha">${escapar(s.fuente)} · ${escapar(s.unidad)} · ` +
    `${s.sentido === "alto_peor" ? "alto = peor" : "alto = mejor"}` +
    `${s.desplazamiento_dias ? ` · desplazada ${entero(s.desplazamiento_dias)} día(s)` : ""}` +
    `</p></div>`
  )).join("");

  partes.push(plegable(
    `Las ${d.series.length} señales por separado`,
    `<p class="explica">Dibujadas por su percentil dentro de la ventana, no por ` +
    `su valor crudo: así se pueden mirar juntas. El número de verdad de cada día ` +
    `viaja en el payload y no se pierde.</p>${series}`,
  ));

  $("vista").innerHTML = partes.join("");
}

// ---------------------------------------------------------------------------
// Vista 2: desfase
// ---------------------------------------------------------------------------

async function pintarDesfase(dias) {
  const d = await pedir(RUTAS.desfase, { dias });
  const partes = [pintarCobertura(d.cobertura, d.ventana)];

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
        `${entero(f.mejor_desfase)} día(s)</b></p>`
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
  return (
    `<table class="tabla"><thead><tr><th>Desfase</th><th>r</th><th>p</th>` +
    `<th>n</th><th>Motivo si no hay</th></tr></thead><tbody>${filas}</tbody></table>`
  );
}

// ---------------------------------------------------------------------------
// Vista 3: impacto
// ---------------------------------------------------------------------------

async function pintarImpacto(dias) {
  const [d, rank] = await Promise.all([
    pedir(RUTAS.impacto, { dias }),
    pedir(RUTAS.ranking, { dias, respuesta: "lower_discomfort" }),
  ]);

  // Las respuestas posibles salen de la propia rejilla. Escritas a mano aquí,
  // añadir un deslizador al `config.yaml` lo dejaría fuera del desplegable y
  // nadie vería nunca su impacto.
  const respuestas = [];
  const vistas = new Set();
  for (const f of d.rejilla) {
    if (!vistas.has(f.respuesta.clave)) {
      vistas.add(f.respuesta.clave);
      respuestas.push(f.respuesta);
    }
  }
  if (!elegido.respuesta || !vistas.has(elegido.respuesta)) {
    elegido.respuesta = respuestas.length ? respuestas[0].clave : null;
  }

  const partes = [pintarCobertura(d.cobertura, d.ventana)];
  partes.push(`<p class="aviso ojo"><strong>Ojo con leer esto como una causa.</strong>` +
    `${escapar(d.advertencia)}</p>`);

  partes.push(
    `<div class="filtro"><label for="resp">Qué le pasó</label>` +
    `<select id="resp">` +
    respuestas.map((r) => (
      `<option value="${escapar(r.clave)}"` +
      `${r.clave === elegido.respuesta ? " selected" : ""}>` +
      `${escapar(r.etiqueta)}</option>`
    )).join("") +
    `</select></div>`,
  );

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

  partes.push(seccionRanking(rank));
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
    `<div class="linea"><label>+${entero(c.dias_despues)} día(s)</label>` +
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
    `<p class="ficha">n = ${entero(c.n)}` +
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

function seccionRanking(rank) {
  if (!rank.ranking.length) {
    return (
      `<h2 class="grupo">Ejercicios ordenados por ${escapar(rank.respuesta.etiqueta.toLowerCase())}</h2>` +
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
          `día(s), que es justo por la que está ordenada esta lista`,
        )
        : barraR(c) +
          (c.r !== null && c.r !== undefined
            ? `<p class="cifra">r = <b>${num(c.r)}</b> a ${escapar(rank.ordenado_por)}</p>`
            : bloqueNa(c.na)) +
          ficha(c)) +
      `<p class="ficha">hecho ${entero(e.veces_hecho)} día(s) en esta ventana</p>` +
      `</article>`
    );
  }).join("");

  return (
    `<h2 class="grupo">Ejercicios ordenados por ${escapar(rank.respuesta.etiqueta.toLowerCase())}</h2>` +
    `<p class="explica">Ordenados por ${escapar(rank.ordenado_por)}. Los que no ` +
    `tienen con qué compararse salen igual, los últimos y con el motivo escrito: ` +
    `un ejercicio que se hace TODOS los días no tiene días sin él, y esconderlo ` +
    `por eso sería esconder justo el que más se hace.</p>` +
    filas
  );
}

// ---------------------------------------------------------------------------
// Vista 4: auditoría
// ---------------------------------------------------------------------------

async function pintarAuditoria(dias) {
  const d = await pedir(RUTAS.auditoria, { dias });
  const g = d.distribucion.global;
  const partes = [pintarCobertura(d.cobertura, d.ventana)];

  partes.push(
    `<article class="tarjeta">` +
    `<h2>El reparto de luces</h2>` +
    `<div class="luces">` +
    `<span class="luz green">${entero(g.green)} verde</span>` +
    `<span class="luz amber">${entero(g.amber)} ámbar</span>` +
    `<span class="luz red">${entero(g.red)} rojo</span>` +
    `<span class="luz nada">${entero(g.sin_decision)} sin decisión</span>` +
    `</div>` +
    (g.porcentaje
      ? `<p class="cifra">${escapar(
          Object.entries(g.porcentaje)
            .map(([k, v]) => `${k}: ${pct(v, 1)}`)
            .join(" · "),
        )}</p>`
      : bloqueNa(
          "no hay ni un día con decisión guardada en esta ventana, así que no " +
          "hay reparto que enseñar: no es un cero, es que no hay denominador",
        )) +
    `<div class="desliza">${calendario(d.dias)}</div>` +
    `<p class="ficha">Un cuadro por día. Los días sin decisión son los huecos ` +
    `con borde: están a la vista porque son parte de lo que hay que auditar.</p>` +
    `<div class="desliza">${barrasSemanales(d.distribucion.por_semana)}</div>` +
    `<p class="ficha">Una barra por semana, sobre los siete días enteros.</p>` +
    `</article>`,
  );

  // Las reglas. Las que nunca dispararon van primero y no al final: son las que
  // hay que mirar, y la distinción entre "se evaluó y no saltó" y "no se pudo
  // evaluar ni una vez" es la que salva esta vista entera.
  const orden = { nunca_evaluada: 0, nunca_disparo: 1, retirada: 2, dispara: 3, sin_historico: 4 };
  const reglas = [...d.reglas].sort(
    (a, b) => (orden[a.estado] ?? 9) - (orden[b.estado] ?? 9),
  );

  partes.push(`<h2 class="grupo">Las ${reglas.length} reglas</h2>`);
  partes.push(reglas.map((r) => (
    `<article class="tarjeta fina estado-${escapar(r.estado)}">` +
    `<h3>${escapar(r.nombre)} <span class="etiqueta-luz ${escapar(r.luz)}">` +
    `${escapar(r.nombre_luz || r.luz)}</span></h3>` +
    (r.descripcion ? `<p class="sub">${escapar(r.descripcion)}</p>` : "") +
    `<p class="lectura">${escapar(r.lectura)}</p>` +
    `<p class="ficha">disparó ${entero(r.veces_disparada)} · mandó ` +
    `${entero(r.veces_determinante)} · evaluada ${entero(r.dias_evaluada)} día(s) · ` +
    `saltada ${entero(r.veces_saltada)}` +
    `${r.ultima_vez ? ` · última vez ${fechaCorta(r.ultima_vez)}` : ""}</p>` +
    (Object.keys(r.le_falto || {}).length
      ? `<p class="ficha">le faltó: ${escapar(
          Object.entries(r.le_falto).map(([k, v]) => `${k} (${v} día(s))`).join(", "),
        )}</p>`
      : "") +
    `</article>`
  )).join(""));

  partes.push(seccionLista(
    "Reglas especiales", d.reglas_especiales,
    (e) => (
      `<h3>${escapar(e.nombre)}${e.declarada ? "" : " <span class=\"retirada\">retirada</span>"}</h3>` +
      `<p class="lectura">${escapar(e.lectura)}</p>` +
      `<p class="ficha">activada ${entero(e.veces_activada)} vez/veces</p>` +
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
      `<h3>${fechaCorta(x.fecha)} ` +
      `<span class="etiqueta-luz ${escapar(x.luz || "")}">${escapar(x.luz || "sin luz")}</span></h3>` +
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
      `<p class="ficha">prescrito ${entero(p.dias_prescrito)} día(s) · ` +
      `${p.subidas.length} subida(s) · ${p.frenos.length} freno(s)</p>`
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
  const partes = [];

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
      `<p class="ficha">n = ${entero(v.n)}</p></div>`
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
