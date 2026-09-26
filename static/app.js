/*
 * El formulario de la mañana.
 *
 * LA DECISIÓN QUE MANDA EN TODO ESTE ARCHIVO
 * ------------------------------------------
 * Un deslizador que nadie ha tocado NO vale 5. Vale nada.
 *
 * Un `<input type=range>` siempre tiene valor, así que lo natural sería empezar
 * todos en el centro y enviar lo que haya. Eso convierte "no lo he contestado"
 * en "estoy justo en la media", que es una respuesta perfectamente normal: el
 * motor decidiría con ella, el semáforo saldría verde y no habría forma de
 * distinguirlo de un día bueno de verdad. Es exactamente el fallo mudo que este
 * sistema entero intenta no tener, y aquí sería el más fácil de cometer.
 *
 * Así que cada deslizador se recuerda si lo han tocado. Los que no, no se
 * envían: el motor tiene un camino para las señales que faltan -las anota como
 * saltadas y lo dice- y ese camino es mejor que un número inventado.
 *
 * El botón no se activa hasta que están todos los obligatorios. `yesterday_rpe`
 * es opcional porque hay días sin entreno el día antes, y eso no es no
 * contestar: es que no hay nada que contestar.
 *
 * Y LA MISMA DECISIÓN, OTRA VEZ, PARA LAS DOS PREGUNTAS DE SÍ/NO
 * --------------------------------------------------------------
 * «¿Te apetece entrenar hoy?» y «¿Vas a entrenar hoy?» tienen TRES estados, no
 * dos: sí, no, y no me lo han dicho. El motor los distingue -`True`, `False` y
 * `None`-, la base los guarda nulables y el mensaje del día mira `is not False`
 * justamente para no confundir el tercero con el segundo.
 *
 * Por eso son DOS BOTONES y no una casilla. Una casilla tiene dos posiciones
 * para tres estados, así que una de las dos tiene que hacer doble papel, y le
 * toca siempre a la de abajo: sin marcar querría decir a la vez "he dicho que
 * no" y "no lo he mirado". Ese aplastamiento es exactamente el que el resto del
 * sistema se ha construido para evitar, y aquí -en el único sitio donde el dato
 * se genera- sería el más barato de cometer y el único imposible de deshacer
 * después: una vez enviado un `false` inventado, ningún análisis puede saber que
 * no lo dijo nadie.
 *
 * Con dos botones, no haber pulsado ninguno no se parece a ninguna respuesta, y
 * lo que se manda es que falta.
 *
 * Y UNA TERCERA VEZ, PARA EL SELECTOR DE QUÉ SE VA A HACER HOY
 * ------------------------------------------------------------
 * El servidor manda una PROPUESTA -la rutina que va a escribir en Hevy si nadie
 * toca nada- y ésta NO se pinta como respuesta. Se pinta como lo que es: una
 * etiqueta que dice «lo que toca hoy» sobre una opción que sigue sin elegir.
 *
 * El orden se pidió así: «preseleccionado con la propuesta del sistema». Y
 * preseleccionar de verdad -meterla en `estado.valores` al abrir- habría sido
 * una línea más corta y habría hecho lo mismo en todo salvo en una cosa: cada
 * mañana que se enviara el formulario sin mirar el selector guardaría
 * `chosen_session: "dia_3"`, o sea una declaración que nadie hizo. Es el mismo
 * aplastamiento de los tres estados que las dos preguntas de arriba se
 * construyeron para evitar, y aquí tiene una consecuencia concreta: el aviso de
 * la mañana siguiente distingue «declaraste el Día 3 y entrenaste el Día 1» de
 * «tocaba el Día 3 y entrenaste el Día 1», y son dos frases distintas porque
 * significan dos cosas distintas.
 *
 * Lo que se entrena no cambia: sin elección, el motor planifica la propuesta.
 * Lo único que cambia es si el histórico sabe que la elegí o solo que no dije
 * nada, y eso, una vez inventado, no se puede deshacer mirando.
 *
 * Por eso el selector es OPCIONAL: no contestarlo no deja el botón gris. Es el
 * único hueco del formulario donde no contestar es un camino previsto.
 */

const API = {
  hoy: "/api/checkin/today",
  enviar: "/api/checkin",
  // Mirar qué saldría, sin que salga. También POST, y aquí el motivo es otro
  // que el de los tres de abajo: no tiene efecto en el mundo, pero sí escribe
  // una fila -la previsualización queda apuntada, que es la mitad de para qué
  // está- y las respuestas viajan en el cuerpo. Un GET las pondría en la URL.
  previsualizar: "/api/preview",
  // El desacuerdo va contra UNA previsualización concreta, la que está en
  // pantalla, y por eso la ruta lleva su `id`. No es cosmética: previsualizar
  // otra vez vuelve a leer Garmin y a pasar por el motor, así que la fila nueva
  // puede traer otra decisión. El juicio tiene que pegarse a la tarjeta que se
  // miró, no a la siguiente que salga.
  desacuerdo: (id) => `/api/preview/${id}/desacuerdo`,
  // Rehacer el día si se decidió sin la noche del reloj. POST: cuando actúa,
  // escribe en Hevy y manda un Telegram. Ver `recalcularSiHaceFalta`.
  recalcular: "/api/decision/recalcular",
  salud: "/api/health",
  // Son POST, no GET, aunque "probar" suene a consulta: mandar un Telegram
  // tiene efecto en el mundo, y las cosas con efecto no se ponen detrás de un
  // verbo que cualquier precargador de enlaces puede disparar solo.
  probar: {
    telegram: "/api/probar/telegram",
    hevy: "/api/probar/hevy",
    garmin: "/api/probar/garmin",
  },
};

// Lo último que se escribió, por si el envío no llega. No es una cola: no se
// reintenta solo. Una cola que reintenta en segundo plano y falla es otra forma
// de creerse que has contestado cuando no.
const BORRADOR = "checkin-borrador";

/* `$`, `escapar` y `fechaLarga` los pone `comun.js`, que `index.html` carga
 * antes que este archivo. Si faltara, lo que se vería es un formulario a medio
 * dibujar sin ningún error en pantalla, así que se comprueba y se rompe aquí. */
if (typeof escapar !== "function") {
  throw new Error("app.js necesita comun.js cargado antes");
}

const estado = {
  dia: null,         // el día SEGÚN EL SERVIDOR, nunca el del móvil
  sliders: [],       // los del config.yaml, tal cual llegan
  preguntas: [],     // las de Sí/No, en su lista aparte y por el mismo motivo
  selector: null,    // qué se va a hacer hoy: las rutinas del ciclo, bici, otro
  // key -> número, booleano o cadena, SOLO de los contestados. Un mismo
  // diccionario para los tres tipos porque el cuerpo que se envía es uno solo y
  // el backend valida la UNIÓN de las tres listas; lo que no se mezcla es cómo
  // se pintan.
  valores: {},
  comentarios: "",
  etiquetaComentarios: "Comentarios",
  enviando: false,
  // Aparte de `enviando` y no un solo `ocupado`: son dos cosas que hay que
  // poder distinguir. Mientras se previsualiza, enviar sigue prohibido por el
  // mismo motivo -una petición a la vez-, pero la frase del botón y lo que pasa
  // al terminar no se parecen en nada.
  previsualizando: false,

  /* LA ANULACIÓN: qué sesión pido yo en vez de la que propone el sistema.
   *
   * `null` es «no he pedido nada», y NO es lo mismo que «he pedido justo lo
   * que proponía». El motor distingue las dos cosas y solo la segunda deja
   * rastro de anulación; mandar siempre el tipo propuesto llenaría la medida
   * de anulaciones que nadie hizo y la dejaría sin poder medir nada.
   *
   * EL FALLO QUE LO TRAE, del 21-09-2026: el sistema puso ámbar por
   * `lumbar_medio` con la lumbar en 5, el usuario se veía bien para la sesión
   * completa, y al enviar se le escribió la reducida en Hevy. Tuvo que
   * arreglarlo a mano. El motor sabía recibir la anulación desde hacía
   * semanas -`SesionPedida`, `SesionAnulada`, `ConfirmacionNecesaria`- y la
   * pantalla no tenía por dónde pedirla: un desacuerdo que obliga a saltarse
   * el sistema es un desacuerdo que no queda registrado y que por tanto no se
   * puede medir nunca.
   */
  sesionPedida: null,
  motivoAnulacion: "",
  // Solo se pone a `true` contestando que sí a la pregunta de subir en rojo, y
  // se vuelve a `false` en cuanto cambia la elección. No es «he leído el
  // aviso» en general: es la respuesta a una pregunta concreta de un día
  // concreto, y arrastrarla convertiría la guarda en un trámite.
  confirmadaEnRojo: false,
};

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

async function arrancar() {
  // Provisional, hasta que conteste el servidor: la fecha buena es la suya.
  $("fecha").textContent = fechaLarga(new Date());

  let datos;
  try {
    const r = await fetch(API.hoy, { cache: "no-store" });
    if (!r.ok) throw new Error(`el servidor ha contestado ${r.status}`);
    datos = await r.json();
  } catch (err) {
    // Sin los deslizadores del servidor no se puede dibujar el formulario, y
    // dibujar uno inventado sería peor: se contestaría a preguntas que el
    // sistema no va a leer.
    $("cargando").innerHTML =
      `<strong>No se ha podido cargar el formulario.</strong><br>` +
      `${escapar(err.message)}<br>` +
      `<span class="tenue">Se necesita conexión con el servidor para saber ` +
      `qué preguntas hay que hacer hoy.</span>`;
    $("cargando").classList.add("mal");
    return;
  }

  estado.dia = datos.day || null;
  // La cabecera dice el día del que se está contestando, que es el del servidor:
  // es su reloj y su zona horaria los que deciden a qué día se apunta esto.
  if (estado.dia) {
    const [a, m, d] = estado.dia.split("-").map(Number);
    $("fecha").textContent = fechaLarga(new Date(a, m - 1, d));
  }

  estado.sliders = datos.sliders || [];
  // Dos listas del servidor y dos aquí. Un solo array con una bandera de tipo
  // obligaría a mirar el tipo antes de pintar nada, y el día que se olvidara
  // saldría una barra de 0 a 10 para «¿Vas a entrenar hoy?».
  estado.preguntas = datos.preguntas || [];
  // Y una tercera cosa, por tercera vez por el mismo motivo. `null` cuando el
  // servidor no lo manda: un `|| {}` dejaría un selector vacío pintado, que se
  // lee como "hoy no hay nada que elegir" en vez de como "esta pantalla es más
  // vieja que el servidor".
  estado.selector = datos.selector || null;
  if (datos.comment_label) estado.etiquetaComentarios = datos.comment_label;
  $("etiqueta-comentarios").textContent = estado.etiquetaComentarios;

  pintarSliders();
  pintarPreguntas();
  pintarSelector();
  // Después de pintar LAS TRES, no entre medias: `recuperar` llama a `fijar`,
  // que busca en el DOM la fila de cada clave. Con las preguntas sin pintar,
  // las respuestas ya enviadas hoy -o el borrador- se perderían en silencio
  // por la salida de "una clave que ya no está en el config".
  recuperar(datos);

  $("cargando").hidden = true;
  $("formulario").hidden = false;
  revisar();

  // CON EL DÍA YA DECIDIDO SE ABRE CON LA DECISIÓN (25/09/2026). El
  // formulario se queda relleno pero plegado detrás de un botón: lo que se
  // viene a mirar a esta hora es qué toca, no las respuestas de esta mañana.
  if (datos.submitted && datos.decision_de_hoy) {
    pintarResultado(datos.decision_de_hoy);
    $("formulario").hidden = true;
    $("cambiar-respuestas").hidden = false;
  }

  // Sin `await`: el formulario ya está pintado y no tiene que esperar a
  // Garmin. Solo con el check-in hecho, que es cuando puede haber un día
  // decidido a ciegas.
  if (datos.submitted) recalcularSiHaceFalta();
}

function cambiarRespuestas() {
  $("formulario").hidden = false;
  $("cambiar-respuestas").hidden = true;
}

/* EL RECÁLCULO AL ABRIR (25/09/2026).
 *
 * Un ámbar «sin datos» prometía recalcular cuando el reloj subiera la noche, y
 * quien lo cumplía eran dos trabajos con hora que caían con el equipo dormido:
 * ese día se quedó ámbar con la noche ya en Garmin. Abrir la aplicación es lo
 * único que garantiza que el servidor está despierto, así que se pregunta aquí.
 *
 * La frase la escribe el servidor y aquí solo se pinta. Casi siempre viene
 * vacía -no había nada que recalcular- y entonces no se pinta nada. */
async function recalcularSiHaceFalta() {
  // Un fallo aquí no se calla, ni de red ni del servidor: si el día seguía a
  // ciegas, lo que queda en pantalla tiene que decir que no se ha podido
  // mirar, no que no hacía falta. `arrancar()` ya ha salido bien, así que
  // nadie más lo diría.
  let d;
  try {
    const r = await fetch(API.recalcular, { method: "POST", cache: "no-store" });
    if (!r.ok) throw new Error(`el servidor ha contestado ${r.status}`);
    d = await r.json();
  } catch (err) {
    aviso(
      `No se ha podido comprobar si el reloj ya ha subido la noche ` +
      `(${err.message}).`,
      "mal",
    );
    return;
  }
  // Si el día se ha recalculado, la tarjeta que había delante ya no es la
  // vigente: se repinta con la que manda el servidor.
  if (d.decision_de_hoy) pintarResultado(d.decision_de_hoy);
  // Sin defecto para el tono: el servidor lo manda siempre junto al aviso. Va
  // en la tarjeta si está a la vista, porque con el formulario plegado un
  // aviso metido dentro no lo vería nadie.
  if (d.aviso) avisoEn($("resultado").hidden ? $("formulario") : $("resultado"), d.aviso, d.tono);
}

/* Lo ya contestado hoy gana sobre el borrador local: es lo que el sistema tiene
 * de verdad. El borrador solo sirve para el caso contrario -se escribió y no
 * llegó a salir- y por eso se avisa cuando se usa. */
function recuperar(datos) {
  if (datos.submitted) {
    for (const [k, v] of Object.entries(datos.values || {})) {
      if (v !== null && v !== undefined) fijar(k, v);
    }
    estado.comentarios = datos.comments || "";
    $("comentarios").value = estado.comentarios;
    aviso(
      "Hoy ya has hecho el check-in. Si lo envías otra vez se recalcula el día " +
      "con las respuestas nuevas.",
      "tenue",
    );
    return;
  }

  let borrador;
  try {
    borrador = JSON.parse(localStorage.getItem(BORRADOR) || "null");
  } catch { borrador = null; }

  if (borrador && estado.dia && borrador.day === estado.dia) {
    for (const [k, v] of Object.entries(borrador.valores || {})) fijar(k, v);
    estado.comentarios = borrador.comentarios || "";
    $("comentarios").value = estado.comentarios;
    aviso(
      "Estas respuestas estaban guardadas en el móvil y NO se habían enviado. " +
      "Revísalas y dale a Enviar.",
      "ojo",
    );
  }
}

// ---------------------------------------------------------------------------
// Los deslizadores
// ---------------------------------------------------------------------------

function pintarSliders() {
  const cont = $("deslizadores");
  cont.innerHTML = "";

  for (const s of estado.sliders) {
    const fila = document.createElement("div");
    fila.className = "slider sin-contestar";
    fila.dataset.key = s.key;

    const opcional = !!s.optional;
    fila.innerHTML = `
      <div class="linea">
        <label for="sl-${escapar(s.key)}">${escapar(s.label || s.key)}</label>
        <output id="out-${escapar(s.key)}" class="valor">—</output>
      </div>
      <input type="range" id="sl-${escapar(s.key)}"
             min="0" max="10" step="1" value="5"
             aria-label="${escapar(s.label || s.key)}">
      <div class="pistas">
        <span>${escapar(s.hint_low || "0")}</span>
        <span>${escapar(s.hint_high || "10")}</span>
      </div>
      ${opcional ? `
      <button type="button" class="no-aplica" data-key="${escapar(s.key)}">
        No entrené ayer
      </button>` : ""}
    `;

    const input = fila.querySelector("input");
    // `input` y no `change`: en un deslizador táctil `change` no salta hasta que
    // se suelta, y hasta entonces el número de al lado diría "—" mientras el
    // dedo lo mueve.
    input.addEventListener("input", () => {
      estado.valores[s.key] = Number(input.value);
      fila.classList.remove("sin-contestar");
      fila.classList.remove("descartado");
      pintarValor(s.key);
      revisar();
      guardarBorrador();
    });

    const noAplica = fila.querySelector(".no-aplica");
    if (noAplica) {
      // Marcar "no entrené ayer" es una respuesta, no un silencio, y por eso
      // tiene botón propio: sin él, dejarlo en blanco y olvidarlo se verían
      // igual.
      noAplica.addEventListener("click", () => {
        delete estado.valores[s.key];
        fila.classList.add("sin-contestar", "descartado");
        pintarValor(s.key);
        revisar();
        guardarBorrador();
      });
    }

    cont.appendChild(fila);
  }
}

// ---------------------------------------------------------------------------
// Las dos preguntas de Sí/No
// ---------------------------------------------------------------------------

/* Dos botones por pregunta. Nunca una casilla: el motivo está entero arriba, y
 * se resume en que una casilla tiene dos posiciones para tres estados.
 *
 * Tampoco un `<select>` con un "—" delante. Se puede, y distingue los tres, pero
 * pone la respuesta a un toque más de distancia y deja el estado sin contestar
 * dentro de la misma lista que las respuestas, con la misma pinta. Aquí no
 * contestar no es una opción del desplegable: es que no hay nada pulsado, y eso
 * se ve de un vistazo desde el otro lado de la habitación.
 */
function pintarPreguntas() {
  const cont = $("preguntas");
  cont.innerHTML = "";

  for (const p of estado.preguntas) {
    const bloque = document.createElement("div");
    bloque.className = "pregunta sin-contestar";
    bloque.dataset.key = p.key;

    const id = `pr-${escapar(p.key)}`;
    bloque.innerHTML = `
      <p class="enunciado" id="${id}">${escapar(p.label || p.key)}</p>
      ${p.nota ? `<p class="nota-pregunta">${escapar(p.nota)}</p>` : ""}
      <div class="si-o-no" role="group" aria-labelledby="${id}">
        <button type="button" data-respuesta="si" aria-pressed="false">Sí</button>
        <button type="button" data-respuesta="no" aria-pressed="false">No</button>
      </div>
    `;

    for (const boton of bloque.querySelectorAll("[data-respuesta]")) {
      boton.addEventListener("click", () => {
        // El booleano sale de CUÁL botón se ha pulsado, no de leer el estado
        // anterior y darle la vuelta. Un `!estado.valores[p.key]` convertiría
        // el primer toque sobre "No" en un "sí" -porque `undefined` negado es
        // `true`- y pulsar dos veces el mismo botón en la respuesta contraria.
        estado.valores[p.key] = boton.dataset.respuesta === "si";
        bloque.classList.remove("sin-contestar");
        pintarRespuesta(p.key);
        revisar();
        guardarBorrador();
      });
    }

    cont.appendChild(bloque);
  }
}

// ---------------------------------------------------------------------------
// Qué se va a hacer hoy
// ---------------------------------------------------------------------------

/* Un botón por opción, y la propuesta MARCADA PERO NO ELEGIDA.
 *
 * Las dos marcas de este bloque son distintas a propósito y no deben poder
 * confundirse mirando:
 *
 *   - `.propuesta` es lo que el sistema va a escribir en Hevy si nadie toca
 *     nada. La pone el servidor, no se puede quitar y no cuenta como respuesta.
 *   - `.elegida` es lo que ha pulsado un dedo. Es la única de las dos que entra
 *     en `estado.valores` y viaja en el POST.
 *
 * Pintarlas iguales -que es lo que pasaría preseleccionando de verdad- haría
 * que "no lo he tocado" y "he elegido justo lo que tocaba" se vieran idénticos,
 * y son el mismo par de estados que el resto de esta pantalla se dedica a no
 * aplastar.
 *
 * La marca de parado sale de `pendiente`, que el servidor ya manda filtrado: si
 * una rutina lleva tanto tiempo sin hacerse que ha caducado, el número
 * desaparece y la opción sigue en su sitio, pulsable igual. Eso es literal del
 * encargo -«que al caducar solo cambie la línea del mensaje, nunca lo que puedo
 * elegir»- y es la diferencia entre un recordatorio y una regañina.
 */
function pintarSelector() {
  const cont = $("selector");
  cont.innerHTML = "";
  // Un servidor que no manda selector -uno viejo, o el día que se quite del
  // config- deja el hueco vacío y el formulario entero sigue funcionando. Lo
  // que no hace es inventarse una lista de rutinas.
  if (!estado.selector) return;

  const sel = estado.selector;
  const bloque = document.createElement("div");
  bloque.className = "selector sin-contestar";
  bloque.dataset.key = sel.key;

  const id = `se-${escapar(sel.key)}`;
  const opciones = (sel.opciones || []).map((o) => {
    const parado = o.pendiente !== null && o.pendiente !== undefined
      ? `<span class="parado">${escapar(cuenta(o.pendiente, "sesión", "sesiones"))}` +
        ` sin hacerlo${o.ultima_vez ? `, desde el ${escapar(fechaMinima(o.ultima_vez))}` : ""}` +
        `</span>`
      : "";
    return (
      // Sin `aria-pressed` aquí, y no es un olvido: lo pone `pintarEleccion` al
      // final de esta misma función. Escrito también en la plantilla habría DOS
      // sitios diciendo si un botón está pulsado, y el de aquí es el que nadie
      // vuelve a mirar: el día que los dos no coincidan, gana el que se ejecuta
      // después y el otro se queda mintiendo en el archivo.
      `<button type="button" data-opcion="${escapar(o.key)}"` +
      ` class="${o.key === sel.propuesta ? "propuesta" : ""}">` +
        `<span class="titulo">${escapar(o.label || o.key)}</span>` +
        (o.key === sel.propuesta
          ? `<span class="toca">lo que toca hoy</span>`
          : "") +
        parado +
      `</button>`
    );
  }).join("");

  bloque.innerHTML = `
    <p class="enunciado" id="${id}">${escapar(sel.label || sel.key)}</p>
    ${sel.nota ? `<p class="nota-pregunta">${escapar(sel.nota)}</p>` : ""}
    <div class="opciones" role="group" aria-labelledby="${id}">${opciones}</div>
  `;

  for (const boton of bloque.querySelectorAll("[data-opcion]")) {
    boton.addEventListener("click", () => {
      // El valor sale del BOTÓN, igual que el booleano de las preguntas sale de
      // cuál se pulsó. Aquí además es lo único que garantiza que lo que se
      // envía sea una de las claves que el servidor ofreció: cualquier otra la
      // rechaza `upsert_checkin`, y desde el móvil un 422 del check-in entero
      // es indistinguible de una avería.
      estado.valores[sel.key] = boton.dataset.opcion;
      bloque.classList.remove("sin-contestar");
      pintarEleccion(sel.key);
      revisar();
      // No sobra porque el clic del selector puede ser el ÚNICO toque de la
      // mañana: se abre el formulario, se elige el Día 2, suena el teléfono y
      // se sale de la app. Sin esta línea eso se pierde entero, y la batería de
      // mutación ya enseñó que un `guardarBorrador()` que falta no pone rojo a
      // nadie salvo que haya un test que lo pida.
      guardarBorrador();
    });
  }

  cont.appendChild(bloque);

  // Y QUIÉN SALE MARCADO LO DECIDE `pintarEleccion`, también -sobre todo- cuando
  // no hay nada elegido. El `aria-pressed="false"` de la plantilla es el estado
  // inicial; mientras fuera además la ÚNICA razón por la que la propuesta no
  // aparece elegida al abrir, esa garantía dependía de que nadie llamara aquí, y
  // no de la comparación que la decide.
  //
  // No es teórico: la batería de mutación cambió esa comparación por «sin elegir
  // se marca la propuesta» y la suite entera siguió verde, porque esa rama no se
  // ejecutaba jamás. Con esta línea, la comparación pasa por aquí todas las
  // mañanas y el test que dice que nadie ha elegido nada puede ponerse rojo.
  pintarEleccion(sel.key);
}

/* Cuál opción está elegida. `v === o` y no `if (v)`, por lo mismo de siempre:
 * sin elegir, `v` es `undefined` y no se marca ninguna. La `.propuesta` no se
 * toca aquí -la pone el servidor y se queda-, así que elegir otra deja las dos
 * marcas a la vista: la que tocaba y la que he dicho. */
function pintarEleccion(key) {
  const bloque = document.querySelector(`.selector[data-key="${CSS.escape(key)}"]`);
  if (!bloque) return;
  const v = estado.valores[key];
  for (const boton of bloque.querySelectorAll("[data-opcion]")) {
    const puesto = v === boton.dataset.opcion;
    boton.classList.toggle("elegida", puesto);
    boton.setAttribute("aria-pressed", String(puesto));
  }
}

/* Poner un valor que viene de fuera: lo ya enviado hoy, o el borrador del móvil.
 *
 * Reparte por el TIPO DE PREGUNTA -mirando qué hay pintado con esa clave-, y
 * una vez repartido comprueba que el valor sea del tipo que toca. Lo segundo no
 * sobra: las dos conversiones automáticas de JavaScript convierten aquí un dato
 * que falta en una respuesta perfectamente creíble, y en direcciones opuestas.
 */
function fijar(key, valor) {
  const escapada = CSS.escape(key);

  const eleccion = document.querySelector(`.selector[data-key="${escapada}"]`);
  if (eleccion) {
    // `String(valor)` haría de un `false` guardado la cadena "false" y de un 0
    // la cadena "0": dos rutinas que no existen, y un check-in entero rechazado
    // con un 422 que desde el móvil se lee como avería del servidor.
    if (typeof valor !== "string") return;
    // Y que sea una de las que HOY hay en pantalla. Un `dia_4` guardado en el
    // borrador de ayer y quitado del ciclo esta mañana no pinta ningún botón:
    // se vería un selector sin elegir mandando una rutina que ya no existe.
    //
    // `for...of` y no `[...].some()`: en un navegador de verdad esto es una
    // `NodeList`, que se recorre pero no tiene `some`. El arnés de los tests
    // devuelve un array y se habría tragado la diferencia.
    let existe = false;
    for (const b of eleccion.querySelectorAll("[data-opcion]")) {
      if (b.dataset.opcion === valor) existe = true;
    }
    if (!existe) return;
    estado.valores[key] = valor;
    eleccion.classList.remove("sin-contestar");
    pintarEleccion(key);
    return;
  }

  const pregunta = document.querySelector(`.pregunta[data-key="${escapada}"]`);
  if (pregunta) {
    // `Boolean(valor)` haría de un 0 guardado un "no" y de un 5 un "sí": una
    // respuesta inventada a la única pregunta del formulario que cambia lo que
    // el sistema escribe esta mañana. Si lo que llega no es un booleano, la
    // pregunta se queda sin contestar y el botón de enviar sigue gris. Eso se
    // ve; una respuesta inventada, no.
    if (typeof valor !== "boolean") return;
    estado.valores[key] = valor;
    pregunta.classList.remove("sin-contestar");
    pintarRespuesta(key);
    return;
  }

  const fila = document.querySelector(`.slider[data-key="${escapada}"]`);
  if (!fila) return;   // una clave guardada que ya no está en el config
  // Y la simétrica, que es peor: `Number(false)` es 0, o sea "ninguna fatiga",
  // "ningún dolor", "ninguna gana". Cero es un extremo del rango, no un hueco,
  // y el motor lo trataría como el dato más rotundo del día.
  if (typeof valor !== "number") return;
  fila.querySelector("input").value = String(valor);
  fila.classList.remove("sin-contestar", "descartado");
  estado.valores[key] = valor;
  pintarValor(key);
}

function pintarValor(key) {
  const fila = document.querySelector(`.slider[data-key="${CSS.escape(key)}"]`);
  const out = fila.querySelector("output");
  if (fila.classList.contains("descartado")) out.textContent = "no entrené";
  else if (fila.classList.contains("sin-contestar")) out.textContent = "—";
  else out.textContent = String(estado.valores[key]);
}

/* Cuál de los dos botones está pulsado.
 *
 * `v === suya` y no `v == suya` ni un `if (v)`: con la pregunta sin contestar
 * `v` es `undefined`, y así los dos botones salen sin marcar, que es lo que
 * hay. Marcar el "No" mientras nadie ha dicho que no es la misma mentira de
 * siempre con otra ropa.
 */
function pintarRespuesta(key) {
  const bloque = document.querySelector(`.pregunta[data-key="${CSS.escape(key)}"]`);
  if (!bloque) return;
  const v = estado.valores[key];
  for (const boton of bloque.querySelectorAll("[data-respuesta]")) {
    const suya = boton.dataset.respuesta === "si";
    const puesto = v === suya;
    boton.classList.toggle("elegida", puesto);
    boton.setAttribute("aria-pressed", String(puesto));
  }
}

/* Qué falta y por qué no se puede enviar todavía. Se dice con nombre y
 * apellidos: un botón gris sin explicación es un callejón sin salida.
 *
 * LAS PREGUNTAS CUENTAN IGUAL QUE LOS DESLIZADORES, y por eso se concatenan las
 * dos listas en vez de mirar solo `estado.sliders`. Dejarlas fuera del recuento
 * habría sido lo cómodo -el formulario se envía igual, el motor tiene camino
 * para el `None`- y habría dejado la mitad de los días sin la respuesta que se
 * añadieron para recoger. Son opcionales para el MOTOR, que sabe seguir sin
 * ellas; no lo son para quien rellena esto.
 *
 * Se respeta `optional` en las dos listas por el mismo motivo por el que no hay
 * ninguna pregunta escrita a mano aquí: quien decide qué es obligatorio es el
 * `config.yaml`. Hoy ninguna de las dos lo lleva.
 *
 * EL SELECTOR NO ENTRA EN LA CUENTA, y eso es una decisión, no un olvido. No
 * contestarlo es un camino previsto: el motor planifica la propuesta y escribe
 * esa rutina en Hevy exactamente igual. Exigirlo pondría el botón gris hasta
 * pulsar una opción, y eso enseñaría a pulsar la marcada para desbloquearlo: el
 * dato recogido dejaría de ser la intención y pasaría a ser el trámite.
 */
function revisar() {
  const faltan = [...estado.sliders, ...estado.preguntas]
    .filter((s) => !s.optional && estado.valores[s.key] === undefined)
    .map((s) => s.label || s.key);

  const p = $("faltan");
  if (faltan.length && !estado.enviando) {
    p.hidden = false;
    p.textContent = `Falta por contestar: ${faltan.join(", ")}.`;
  } else {
    p.hidden = true;
  }
  $("enviar").disabled =
    faltan.length > 0 || estado.enviando || estado.previsualizando;

  // Y el de previsualizar NO mira `faltan`, que es toda la diferencia entre los
  // dos y está razonada entera en `index.html`: previsualizar con una respuesta
  // sin contestar es una pregunta legítima -el motor sabe seguir sin ella y lo
  // dice- y ésta es la única pantalla donde se puede hacer sin consecuencias.
  //
  // Lo único que lo apaga es que haya una petición en marcha. Se decide aquí, y
  // no en `previsualizar()`, para que los dos botones tengan un solo sitio que
  // diga cuándo están grises: repartido, el día que se añada un tercer estado
  // uno de los dos se queda sin enterarse.
  $("previsualizar").disabled = estado.enviando || estado.previsualizando;
}

/* El borrador se marca con el día QUE DIGA EL SERVIDOR, no con el del móvil.
 * `new Date().toISOString()` da la fecha en UTC, y España va por delante: un
 * borrador escrito a la una de la mañana se guardaría con la fecha de ayer y al
 * recargar no casaría con `datos.day`. Se perdería en silencio, que es la
 * dirección menos mala pero sigue siendo perder lo que el usuario ha escrito. */
function guardarBorrador() {
  if (!estado.dia) return;   // sin saber de qué día es, no se guarda
  estado.comentarios = $("comentarios").value;
  try {
    localStorage.setItem(BORRADOR, JSON.stringify({
      day: estado.dia,
      valores: estado.valores,
      comentarios: estado.comentarios,
    }));
  } catch { /* sin sitio en el móvil: no es motivo para romper el formulario */ }
}

// ---------------------------------------------------------------------------
// Enviar
// ---------------------------------------------------------------------------

async function enviar(ev) {
  ev.preventDefault();
  if (estado.enviando || estado.previsualizando) return;

  // LA TARJETA DE LA PREVISUALIZACIÓN SE RETIRA AQUÍ, y no es limpieza.
  //
  // Lo que dice esa tarjeta -«todavía no se ha escrito nada»- es una afirmación
  // sobre un momento, no sobre el día, y deja de ser cierta exactamente al
  // pulsar este botón. Dejándola puesta quedarían las dos a la vez en la misma
  // pantalla: arriba la que jura que no se ha tocado nada y abajo la que cuenta
  // que se ha escrito la rutina y salido el Telegram. Y la de arriba es la que
  // se leyó hace diez segundos, así que es la que suena a verdad.
  //
  // Es el mismo fallo del 18 de septiembre con otra ropa: una frase que era
  // cierta cuando se escribió y que nadie retiró cuando dejó de serlo.
  //
  // Se retira ANTES de saber cómo acaba el envío, y a propósito: si el POST
  // falla, lo que se pintará abajo es que no se ha enviado -eso ya lo dice
  // `resultadoMal` con todas las letras-, y volver a colgar la previsualización
  // de antes no añadiría nada que no esté ahí. Lo que no puede pasar es que un
  // envío a medias conviva con una tarjeta que promete que no ha pasado nada.
  $("previsualizacion").hidden = true;

  estado.enviando = true;
  revisar();
  $("enviar").textContent = "Enviando…";

  const cuerpo = { ...estado.valores, ...loQuePido() };
  const texto = $("comentarios").value.trim();
  if (texto) cuerpo.comments = texto;

  let r, datos;
  try {
    r = await fetch(API.enviar, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuerpo),
    });
    datos = await r.json().catch(() => ({}));
  } catch (err) {
    // No ha llegado a salir. El borrador se queda donde está, y se dice que NO
    // se ha enviado: dar por bueno lo que no ha salido es la peor mentira que
    // puede contar esta pantalla.
    guardarBorrador();
    resultadoMal(
      "No se ha enviado",
      `No se ha podido hablar con el servidor (${escapar(err.message)}). ` +
      `Tus respuestas siguen guardadas en el móvil: vuelve a intentarlo cuando ` +
      `tengas conexión.`,
    );
    return terminar();
  }

  // La pregunta del rojo también llega por aquí, y hay que distinguirla de un
  // rechazo. No es «el servidor ha dicho que no»: es «contéstame esto». Si
  // cayera en el `resultadoMal` de abajo, el usuario leería que su check-in ha
  // sido rechazado -y no lo ha sido, no se ha guardado nada- y la subida en
  // rojo se volvería imposible de pedir desde el envío.
  const pregunta = esConfirmacionEnRojo(r, datos);
  if (pregunta) {
    guardarBorrador();
    pedirConfirmacionEnRojo(pregunta);
    $("previsualizacion").scrollIntoView({ behavior: "smooth", block: "start" });
    return terminar();
  }

  if (!r.ok) {
    const detalle = typeof datos.detail === "string"
      ? datos.detail
      : JSON.stringify(datos.detail || datos);
    guardarBorrador();
    resultadoMal(
      `El servidor ha rechazado el check-in (${r.status})`,
      escapar(detalle),
    );
    return terminar();
  }

  // A partir de aquí el check-in SÍ está guardado, así que el borrador sobra.
  localStorage.removeItem(BORRADOR);
  pintarResultado(datos);
  terminar();
}

function terminar() {
  estado.enviando = false;
  $("enviar").textContent = "Enviar otra vez";
  revisar();
  $("resultado").scrollIntoView({ behavior: "smooth", block: "start" });
}

/* Cómo se traduce a castellano el estado de cada canal cuando el día se cayó.
 *
 * Esto no calcula nada: es un diccionario de palabra a frase. El número, o aquí
 * el hecho, lo decide el servidor; la pantalla sólo lo escribe con letras que se
 * entiendan sin saber qué es un `read_only`. */
const QUEDO_HEVY = {
  ok: "sí se ha escrito la rutina",
  skipped: "no se ha tocado",
  dry_run: "no se ha tocado, estaba en modo prueba",
  read_only: "no se ha tocado, estaba en modo solo lectura",
  stale: "no se ha tocado, había una escritura anterior a medias",
  reverted: "se escribió y se ha deshecho",
  error: "se ha intentado escribir y ha fallado",
};

const QUEDO_TELEGRAM = {
  sent: "sí se ha enviado el mensaje",
  skipped: "no se ha enviado",
  dry_run: "no se ha enviado, estaba en modo prueba",
  error: "se ha intentado enviar y ha fallado",
};

/* `null` no es `skipped`, y ésa es toda la gracia de esta función.
 *
 * «No se ha tocado» es una afirmación y «no se sabe» es la ausencia de una, y
 * confundirlas es lo que rompió la pantalla el 18 de septiembre de 2026. Si el
 * servidor no manda el estado, aquí no se rellena el hueco con la opción
 * optimista: se dice que no se sabe y el usuario va a mirar. */
function comoQuedo(tabla, estado) {
  if (estado === null || estado === undefined) return "no se sabe";
  return tabla[estado] || estado;
}

/* El servidor puede contestar 200 con `decided: false`: el check-in se guardó y
 * la decisión falló. Eso NO es un éxito y no puede parecerlo, porque el usuario
 * cerraría el móvil convencido de que ya está y se enteraría al no llegarle el
 * mensaje.
 *
 * Y TAMPOCO PUEDE PARECER MÁS LIMPIO DE LO QUE FUE
 * ------------------------------------------------
 * Aquí había una frase fija que decía que no se había tocado Hevy ni enviado
 * ningún mensaje, y era una frase que esta pantalla no estaba en condiciones de
 * firmar. Hevy y Telegram se tocan FUERA de la transacción: cuando el fallo
 * llega tarde, el `rollback` borra la fila de la base de datos y deja la rutina
 * escrita y el mensaje entregado.
 *
 * El 18 de septiembre de 2026 pasó exactamente eso -reventó al apuntar el aviso,
 * con el Telegram ya en el móvil- y la pantalla dijo que no se había mandado
 * nada, teniendo el usuario el mensaje delante. Ahora se escribe lo que el
 * servidor informa, y si no informa se dice que no se sabe. */
function pintarResultado(d) {
  const caja = $("resultado");
  caja.hidden = false;

  if (!d.decided) {
    const hevy = comoQuedo(QUEDO_HEVY, d.hevy);
    const tg = comoQuedo(QUEDO_TELEGRAM, d.telegram);
    const aOjo = hevy === "no se sabe" || tg === "no se sabe";
    caja.className = "resultado mal";
    caja.innerHTML = `
      <h2>Guardado, pero sin decidir</h2>
      <p>El check-in <strong>sí</strong> se ha guardado. Lo que ha fallado es la
         decisión del día.</p>
      <dl>
        <dt>Hevy</dt><dd>${escapar(hevy)}</dd>
        <dt>Telegram</dt><dd>${escapar(tg)}</dd>
      </dl>
      ${aOjo ? `
        <p class="aviso ojo">Donde pone «no se sabe» es que el fallo no dejó rastro de
           hasta dónde llegó la mañana. Mira la rutina y el móvil antes de
           volver a enviar, no vaya a hacerse dos veces.</p>` : ""}
      <pre>${escapar(d.error || "sin detalle")}</pre>`;
    return;
  }

  const nombre = { green: "Verde", amber: "Ámbar", red: "Rojo" };
  caja.className = `resultado semaforo-${escapar(d.light)}`;
  // Hevy y Telegram en palabras, como en la rama del fallo. Aquí se pintaba el
  // código tal cual -«Hevy: ok», «Telegram: sent»-, y además `null` salía como
  // «—», que no dice si se tocó o no se sabe. `comoQuedo` sí lo distingue.
  caja.innerHTML = `
    <h2><span class="punto"></span>${escapar(nombre[d.light] || d.light)}</h2>
    <p class="sesion">${escapar(d.session || "")}</p>
    ${d.cuando ? `<p class="tenue">${escapar(d.cuando)}</p>` : ""}
    <dl>
      <dt>Hevy</dt><dd>${escapar(comoQuedo(QUEDO_HEVY, d.hevy))}</dd>
      <dt>Telegram</dt><dd>${escapar(comoQuedo(QUEDO_TELEGRAM, d.telegram))}</dd>
    </dl>
    ${(d.problems && d.problems.length) ? `
      <div class="problemas">
        <h3>Ha salido, pero con avisos</h3>
        <ul>${d.problems.map((p) => `<li>${escapar(p)}</li>`).join("")}</ul>
      </div>` : ""}`;
}

function resultadoMal(titulo, cuerpo) {
  const caja = $("resultado");
  caja.hidden = false;
  caja.className = "resultado mal";
  caja.innerHTML = `<h2>${escapar(titulo)}</h2><p>${cuerpo}</p>`;
}

function aviso(texto, clase) {
  avisoEn($("formulario"), texto, clase);
}

function avisoEn(caja, texto, clase) {
  const p = document.createElement("p");
  p.className = `aviso ${clase}`;
  p.textContent = texto;
  caja.prepend(p);
}

// ---------------------------------------------------------------------------
// Previsualizar
// ---------------------------------------------------------------------------
/*
 * PARA QUÉ ESTÁ ESTA PANTALLA
 * ---------------------------
 * Para calibrar. No para forzar el resultado que apetece: para pulsar el botón,
 * ver qué contesta el sistema, y que con el tiempo lo que dice el sistema y lo
 * que uno sabe de sí mismo acaben coincidiendo. Por eso cada previsualización
 * se guarda con las respuestas que la generaron, por eso la segunda no tapa a
 * la primera -la diferencia entre las dos es el dato- y por eso se puede
 * apuntar «no estoy de acuerdo» sin tocar ninguna respuesta.
 *
 * LO PRIMERO QUE TIENE QUE DECIR LA TARJETA ES QUE NO HA PASADO NADA
 * -----------------------------------------------------------------
 * Fue la condición explícita del encargo: «que no me quede duda de si ya está
 * hecho o no». Un botón que se pulsa justo encima del de enviar y que contesta
 * con un semáforo, una sesión y unas cargas se parece muchísimo a un día ya
 * decidido, y si la tarjeta no lo desmiente en la primera línea, el día que se
 * pulse el que no era nadie se va a enterar hasta que no llegue el Telegram.
 *
 * Y NO SE DESMIENTE CON UNA FRASE FIJA. Eso es lo que había en `pintarResultado`
 * hasta el 18 de septiembre de 2026 -una frase clavada diciendo que no se había
 * tocado nada, encima de un día en que sí- y es la razón de que aquí se escriba
 * lo que el servidor INFORMA, hecho por hecho, con «no se sabe» cuando no
 * informa. La cabecera tranquilizadora solo sale si los cinco hechos dicen que
 * no se ha tocado nada; en cuanto uno no lo dice, o no se sabe, sale la otra.
 *
 * NI UN NÚMERO SE CALCULA AQUÍ
 * ----------------------------
 * El semáforo, el motivo, el título de la sesión, los cambios de carga y la
 * recomendación de bici llegan ya redactados por el motor, que es quien tiene
 * el `config.yaml`, el histórico y los tests. Esta función elige dónde va cada
 * frase y no escribe ninguna. Hasta `revision` viene resuelto del servidor
 * -podría salir de `seq > 1`- para que la pantalla no tenga que saber que la
 * numeración empieza en uno.
 */

/* Los cuatro hechos por los que uno se pregunta «¿ya está hecho?», con la
 * palabra que les corresponde en cada estado.
 *
 * `si` y `no` escritos los dos, en vez de un `no` y dar por hecho el contrario:
 * el caso que importa de verdad es el `si`, o sea la previsualización que SÍ ha
 * guardado el check-in, que no debería poder pasar nunca y que es exactamente
 * lo que hay que poder leer el día que pase. */
const HECHOS_DEL_MIRAR = [
  {
    clave: "checkin_guardado",
    que: "Tus respuestas de hoy",
    no: "no se han guardado",
    si: "SÍ se han guardado",
  },
  {
    clave: "decision_guardada",
    que: "La decisión del día",
    no: "no se ha tomado",
    si: "SÍ se ha tomado",
  },
];

function hecho(h, valor) {
  // El mismo criterio que `comoQuedo`, y por el mismo motivo: un hueco no se
  // rellena con la opción optimista. `undefined` aquí es un servidor que no lo
  // ha dicho, y no decirlo no es decir que no.
  if (valor === false) return h.no;
  if (valor === true) return h.si;
  return "no se sabe";
}

/* Si un canal se ha quedado sin tocar, DECIDIDO POR LO QUE NO DICE.
 *
 * Las claves de `QUEDO_HEVY` y `QUEDO_TELEGRAM` son los estados de una
 * EJECUCIÓN: escrita, revertida, en seco, fallada... Una previsualización no
 * ejecuta, así que lo que manda -«sin tocar»- no es ninguna de ellas, y eso es
 * lo que se comprueba.
 *
 * Escrito así y no comparando con la cadena «sin tocar» a propósito: esa cadena
 * la elige el servidor, y repetirla aquí pondría la afirmación más importante
 * de esta pantalla a depender de que dos ficheros escriban igual dos palabras,
 * sin nada que lo comprobara. Preguntando por lo contrario, cualquier palabra
 * nueva que el servidor invente para «no se tocó» sigue funcionando, y
 * cualquier estado de ejecución de verdad enciende el aviso aunque sea uno que
 * todavía no existe.
 *
 * `null` no cuenta como intacto, por lo de siempre: no se sabe no es una
 * garantía, y ésta es la línea de la pantalla que menos puede permitirse
 * afirmar de más. */
function canalIntacto(tabla, valor) {
  if (valor === null || valor === undefined) return false;
  return tabla[valor] === undefined;
}

/* La cabecera: qué se ha tocado, según lo que informe la respuesta.
 *
 * La usan las DOS tarjetas -la buena y la del error-, y ése es medio motivo de
 * que esté aparte. El otro es el 502 de Garmin: ahí no hay previsualización que
 * enseñar, pero la pregunta «¿ha pasado algo?» sigue teniendo la misma
 * respuesta y sigue haciendo la misma falta. El servidor manda el desglose en
 * las tres salidas -la buena, el 409 y el 502- precisamente para esto.
 */
function cabeceraDeLoQueSeHaTocado(d) {
  const hevy = comoQuedo(QUEDO_HEVY, d.hevy);
  const tg = comoQuedo(QUEDO_TELEGRAM, d.telegram);

  const intacto =
    d.ejecutado === false &&
    d.checkin_guardado === false &&
    d.decision_guardada === false &&
    canalIntacto(QUEDO_HEVY, d.hevy) &&
    canalIntacto(QUEDO_TELEGRAM, d.telegram);

  const filas = HECHOS_DEL_MIRAR.map(
    (h) => `<dt>${escapar(h.que)}</dt><dd>${escapar(hecho(h, d[h.clave]))}</dd>`,
  ).join("");

  // LA QUINTA LÍNEA, que es la que impide que la cabecera sea una mentira
  // pequeña. «No se ha escrito nada» no es exacto: la previsualización SÍ se
  // apunta, y esa fila es la mitad de para qué existe el botón. Callarlo para
  // que el mensaje quedara más redondo sería empezar esta pantalla haciendo
  // justo lo que vino a arreglar.
  const apuntada = d.previsualizacion_guardada === true
    ? `sí, queda apuntada${d.seq ? ` (la nº ${escapar(d.seq)} de hoy)` : ""}`
    : d.previsualizacion_guardada === false
      ? "no se ha llegado a apuntar"
      : "no se sabe";

  return (
    (intacto
      ? `<h2>Todavía no se ha escrito nada</h2>
         <p>Esto es solo una mirada: el sistema ha pensado el día con estas
            respuestas y te lo enseña. Para que ocurra de verdad hay que darle
            a <strong>Enviar</strong>.</p>`
      : `<h2>Ojo: esto no ha sido solo mirar</h2>
         <p>Algo de lo que esta pantalla da por no tocado no consta como tal.
            Léelo antes de volver a enviar, no vaya a hacerse dos veces.</p>`) +
    `<dl class="lo-tocado">
       ${filas}
       <dt>Hevy</dt><dd>${escapar(hevy)}</dd>
       <dt>Telegram</dt><dd>${escapar(tg)}</dd>
       <dt>Esta previsualización</dt><dd>${apuntada}</dd>
     </dl>`
  );
}

/* Las tres durezas, en castellano. Diccionario de palabra a frase, como
 * `QUEDO_HEVY`: aquí no se decide nada, se traduce. Con el valor crudo de
 * respaldo, que es peor de leer y mejor que un hueco: una dureza nueva en el
 * motor saldría como `deload` y se entendería; sustituida por "" no se vería
 * que falta. */
const DUREZA = {
  full: "sesión completa",
  reduced: "sesión reducida",
  recovery: "sesión de recuperación",
};

const NOMBRE_DE_LA_LUZ = { green: "Verde", amber: "Ámbar", red: "Rojo" };

function bloqueLista(titulo, items) {
  if (!items || !items.length) return "";
  return (
    `<h3>${escapar(titulo)}</h3>` +
    `<ul>${items.map((x) => `<li>${escapar(x)}</li>`).join("")}</ul>`
  );
}

/* Por qué ha salido este color.
 *
 * La regla que disparó viene en `trigger_rule` y su detalle en la entrada de
 * `fired_rules` que se llama igual. Es una BÚSQUEDA POR NOMBRE, no una cuenta:
 * las frases ya están escritas: aquí solo se emparejan. Es exactamente lo que
 * hace el mensaje de Telegram con los mismos dos campos.
 *
 * `for...of` y no `.find()`, por lo mismo que en `fijar`: aquí llega un array
 * de JSON de verdad, pero la costumbre de no depender de los métodos cómodos es
 * la que evitó el `NodeList.some` que sí habría reventado en el móvil.
 */
function porQue(d) {
  const dec = d.decision || {};
  const lineas = [];

  /* LA REGLA QUE DECIDIÓ EL COLOR, LEÍDA DE UN SOLO SITIO.
   *
   * El servidor la manda DOS VECES: suelta en la raíz de la respuesta y otra
   * vez dentro de `decision`. Aquí se leía solo la de la raíz, y eso no es un
   * detalle de estilo: es un dato con dos fuentes y un sitio donde pueden
   * discrepar. Se prefiere la de la raíz porque es la que el endpoint calcula
   * para esta respuesta, y se cae a la de dentro cuando no está.
   */
  const disparadora = d.trigger_rule || dec.trigger_rule || null;
  const saltadas = dec.fired_rules || [];

  /* Y LA FRASE SE DECIDE POR LAS REGLAS, NO POR QUE LLEGUE EL CAMPO.
   *
   * Decía «Ninguna regla ha saltado hoy» cuando faltaba `trigger_rule`, que es
   * OTRO HECHO del que la frase afirma. Con la regla disparadora ausente y
   * `fired_rules` lleno -un cliente que pase solo `decision`, o el día que
   * alguien quite el campo duplicado de la raíz por ordenar- la tarjeta
   * imprimía «Ninguna regla ha saltado hoy» y justo debajo la regla que había
   * saltado, con su detalle.
   *
   * No es una posibilidad teórica: salió al renderizar esta tarjeta a mano con
   * el payload real guardado en `previews`, que no lleva la copia de la raíz.
   * Y sale en el único bloque de la pantalla cuyo trabajo es explicar por qué
   * el sistema ha decidido lo que ha decidido: el sitio donde una contradicción
   * cuesta más cara.
   *
   * Atada al recuento de reglas, la frase no puede contradecir a la lista que
   * tiene debajo, porque sale de ella.
   */
  if (!disparadora && !saltadas.length) {
    lineas.push("Ninguna regla ha saltado hoy");
  }

  if (disparadora) {
    const disparo = saltadas.find((r) => r.name === disparadora) || null;
    const det = disparo && disparo.detail && disparo.detail.length
      ? `: ${disparo.detail.join("; ")}`
      : "";
    lineas.push(`${disparadora}${det}`);
  }

  // Las demás reglas que saltaron. El mensaje de la mañana solo nombra la que
  // decidió el color, y ahí está bien -es un mensaje, no un informe-; aquí se
  // está calibrando, y «han saltado tres» frente a «ha saltado una por los
  // pelos» son dos días que no se parecen en nada aunque salgan del mismo color.
  for (const r of saltadas) {
    if (r.name === disparadora) continue;
    const det = r.detail && r.detail.length ? `: ${r.detail.join("; ")}` : "";
    lineas.push(`${r.name}${det}`);
  }

  for (const n of dec.notes || []) lineas.push(n);

  return lineas;
}

/* Las reglas que NO se pudieron mirar por falta de datos.
 *
 * Es lo que contesta «¿y si hoy no contesto esto?», que es media razón de que
 * el botón de previsualizar no se ponga gris cuando falta una respuesta. Sin
 * esta lista, un día con la mitad del formulario en blanco sale verde y parece
 * un día bueno, cuando lo que pasa es que no se ha podido mirar.
 *
 * Plegada en un `<details>` porque la mayoría de los días está vacía o dice
 * algo que ya se sabe, y desplegada taparía lo que sí se viene a ver. */
/* Lo que se añade al cuerpo de las DOS rutas cuando hay anulación.
 *
 * Una sola función y no dos trozos parecidos en `previsualizar()` y en
 * `enviar()`: son el mismo hecho, y el día que se separen lo que se mira y lo
 * que se manda dejarán de ser lo mismo sin que nada se queje. Es justo el
 * fallo que la anulación viene a resolver, cometido al implementarla.
 *
 * Con `sesionPedida` a `null` devuelve un objeto VACÍO, no `{requested_session:
 * null}`. El motor distingue «no se pidió nada» de «se pidió lo propuesto», y
 * solo el segundo deja rastro; mandar la clave siempre convertiría cada día
 * corriente en una anulación a efectos de las cuentas.
 */
function loQuePido() {
  if (!estado.sesionPedida) return {};
  const fuera = { requested_session: estado.sesionPedida };
  const motivo = (estado.motivoAnulacion || "").trim();
  if (motivo) fuera.override_reason = motivo;
  if (estado.confirmadaEnRojo) fuera.confirm_upgrade = true;
  return fuera;
}

/* Los cuatro tipos de sesión, en el orden en que los entiende el motor.
 *
 * El orden es el de `DUREZA` en `session_builder.py` -recuperación, reducida,
 * completa- y no el alfabético ni el de "lo más probable primero": la pantalla
 * enseña una escala, y una escala desordenada se lee mal justo el día que hay
 * que elegir deprisa. `null` va el primero porque es el defecto.
 */
const TIPOS_DE_SESION = [
  {tipo: null, label: "Lo que propone el sistema"},
  {tipo: "recovery", label: "Recuperación"},
  {tipo: "reduced", label: "Sesión reducida"},
  {tipo: "full", label: "Sesión completa"},
];

const NOMBRE_DEL_TIPO = {
  full: "sesión completa",
  reduced: "sesión reducida",
  recovery: "recuperación",
};

/* EL CONTROL PARA PEDIR OTRA COSA.
 *
 * Va DESPUÉS de «Lo que propondría» y no antes, y ese orden es la mitad de lo
 * que hace: primero se lee qué propone el sistema y por qué, y solo después se
 * elige. Puesto arriba se convertiría en un menú que se contesta antes de
 * haber leído nada, y entonces lo que mide -cuántas veces y en qué umbral se
 * discrepa- mediría sobre todo la prisa.
 *
 * Cambiar la elección VUELVE A PREVISUALIZAR, y eso tampoco es un adorno: sin
 * ello el usuario elige «completa» a ciegas y solo ve lo que ha pedido después
 * de enviarlo, que es exactamente el momento en que ya no se puede deshacer.
 * Además cada mirada queda apuntada con su anulación, que es de donde sale la
 * medida.
 */
function bloqueEleccion(d) {
  // Con un «no voy a entrenar» contestado, elegir el tipo de sesión no
  // significa nada: no hay sesión que endurecer ni que rebajar. Y no es solo
  // que sobre en pantalla: cada opción que se pulsara aquí quedaría apuntada
  // como una ANULACIÓN, contando hacia el umbral de diez de una regla con la
  // que el usuario ni siquiera está discutiendo. El contador mediría, en
  // parte, los días que no se entrena.
  if ((d.decision || {}).va_a_entrenar === false) return "";
  // Ni en un día de bici: la dureza que se elige aquí es la del gimnasio, y
  // ese día no se va. El motor ya no la aplica (`session_builder`, 26/09/2026);
  // enseñar el control sería ofrecer un botón que no hace nada.
  if (esDiaDeBici(d)) return "";

  const propuesto = (d.decision && d.decision.session && d.decision.session.kind) || null;
  const opciones = TIPOS_DE_SESION.map((o) => {
    const esDefecto = o.tipo === null;
    const nombre = esDefecto && propuesto
      ? `Lo que propone el sistema (${NOMBRE_DEL_TIPO[propuesto] || propuesto})`
      : o.label;
    return (
      `<button type="button" data-sesion="${escapar(o.tipo === null ? "" : o.tipo)}">` +
        `<span class="titulo">${escapar(nombre)}</span>` +
        (esDefecto ? `<span class="toca">lo de siempre</span>` : "") +
      `</button>`
    );
  }).join("");

  return (
    `<div class="eleccion-sesion selector">` +
      `<h3>Qué voy a hacer</h3>` +
      `<p class="tenue">Si eliges algo distinto, en Hevy se escribe lo tuyo. ` +
      `Se registra qué proponía el sistema y qué elegiste.</p>` +
      `<div class="opciones">${opciones}</div>` +
      `<label class="motivo-anulacion">` +
        `<span>¿Por qué? Una línea basta, y puede quedarse en blanco.</span>` +
        `<textarea class="texto-anulacion" rows="2" ` +
        `placeholder="Lo que tú sabes y el sistema no"></textarea>` +
      `</label>` +
    `</div>`
  );
}

/* Cuelga los oyentes del control. Aparte de la plantilla por lo mismo que
 * `pintarDesacuerdo`: un `id` escrito dentro de una plantilla no está en
 * `index.html`, y buscar por clase DESDE LA TARJETA es lo único que garantiza
 * que se escuche el control de ÉSTA y no el de una previsualización anterior
 * que se quedó colgando. */
function pintarEleccionSesion(caja) {
  const bloque = caja.querySelector(".eleccion-sesion");
  if (!bloque) return;

  const marcar = () => {
    for (const b of bloque.querySelectorAll("[data-sesion]")) {
      const suyo = b.dataset.sesion || null;
      const elegido = suyo === estado.sesionPedida;
      b.classList.toggle("elegida", elegido);
      b.setAttribute("aria-pressed", elegido ? "true" : "false");
    }
  };
  marcar();

  const texto = bloque.querySelector(".texto-anulacion");
  texto.value = estado.motivoAnulacion || "";
  texto.addEventListener("input", () => {
    estado.motivoAnulacion = texto.value;
  });

  for (const b of bloque.querySelectorAll("[data-sesion]")) {
    b.addEventListener("click", () => {
      const pedido = b.dataset.sesion || null;
      if (pedido === estado.sesionPedida) return;
      estado.sesionPedida = pedido;
      // La confirmación de rojo muere con el cambio de elección: era la
      // respuesta a «¿seguro que quieres subir HOY?», y cambiar de opción
      // hace que esa pregunta ya no sea la que se contestó.
      estado.confirmadaEnRojo = false;
      marcar();
      previsualizar();
    });
  }
}

function sinDatos(d) {
  const dec = d.decision || {};
  const cojas = [];
  for (const r of dec.skipped_rules || []) {
    if (r.missing && r.missing.length) {
      cojas.push(`${r.name}: falta ${r.missing.join(", ")}`);
    }
  }
  if (!cojas.length) return "";
  return (
    `<details class="sin-datos">` +
    `<summary>No se han podido mirar ${escapar(cojas.length)} reglas</summary>` +
    `<ul>${cojas.map((x) => `<li>${escapar(x)}</li>`).join("")}</ul>` +
    `</details>`
  );
}

/* La sesión que se escribiría, y todo lo que se le ha hecho por el camino.
 *
 * Tres listas distintas y no una, porque son tres cosas distintas y el usuario
 * pidió los «cambios de carga» por su nombre: `progression.changes` es lo que
 * SUBE hoy, `session.changes` es lo que se le ha recortado a la rutina base y
 * `session.dropped` es lo que se ha caído del todo. Juntas en un montón se
 * leerían como veinte modificaciones de la misma clase. */
/* HOY NO ENTRENAS: LA TARJETA DEJA DE PRESCRIBIR.
 *
 * `message.py` lleva esto resuelto desde hace tiempo -`prescribe =
 * va_a_entrenar is not False`, y con un «no voy» el Telegram cuenta el día en
 * vez de darte el plan-. La tarjeta de la PWA no se enteró, así que la misma
 * decisión salía prescribiendo por una pantalla y no por la otra.
 *
 * Lo que se veía el 23-09-2026 después de contestar que no: «Lo que
 * propondría: Día 2, sesión completa», la lista de cambios sobre la rutina,
 * «Hoy se quedan fuera: Peso muerto», los apuntes de la sesión y el control
 * para elegir qué sesión hacer. Todo ello debajo de una nota que ya decía
 * «hoy no entrenas». La pantalla se contradecía y encima daba instrucciones
 * para un entreno que el usuario acababa de decir que no iba a hacer.
 *
 * SE COMPARA CON `=== false` Y NO CON `!`. Son tres estados y no dos: `null`
 * es «no lo has contestado» -o un día del archivo anterior a que la pregunta
 * existiera- y tiene que seguir prescribiendo. Escrito `if
 * (!dec.va_a_entrenar)`, la tarjeta dejaría de proponer sesión todos los días
 * en que no se rellena el formulario, que son la mayoría, y en silencio. Es
 * literalmente el mismo aviso que lleva escrito `message.py` en su sitio.
 *
 * Y LAS DOS FRASES SON LAS SUYAS, no unas nuevas. Contestan las dos preguntas
 * que quedan en el aire -«¿pierdo el turno?» y «¿y si cambio de idea a las
 * siete?»- y ya están redactadas y razonadas allí. Dos pantallas contando lo
 * mismo con palabras distintas es una de ellas envejeciendo.
 */
function bloqueSinEntreno(d) {
  const dec = d.decision || {};
  const ses = dec.session || {};
  const cual = ses.title
    ? `${escapar(ses.title)} sigue siendo la siguiente`
    : "la rutina que tocaba sigue siendo la siguiente";
  return (
    `<h3>Hoy no entrenas</h3>` +
    `<p class="sesion">${cual}: la rotación no se mueve hasta que se haga.</p>` +
    `<p class="tenue">La rutina se escribe en Hevy de todas formas, por si ` +
    `cambias de idea.</p>`
  );
}

/* EL DÍA DE BICI NO TIENE SESIÓN DE GIMNASIO QUE PROPONER (26/09/2026).
 *
 * Elegido «Bici» en el check-in, la tarjeta decía «Lo que propondría: Día 1»
 * con sus cambios de carga, el control de dureza debajo y la bici al final,
 * en condicional: «Si sales hoy». El usuario ya había dicho que salía y que
 * no iba al gimnasio. Ahora el motor lo sabe -sesión `bici`, sin ejercicios
 * y sin Hevy- y la pantalla lo lee de ahí, del `kind`, no de la respuesta
 * del formulario: si el rojo manda sobre la bici, lo que llega es la
 * recuperación, y eso es lo que hay que enseñar. */
function esDiaDeBici(d) {
  return ((d.decision || {}).session || {}).kind === "bici";
}

function bloqueSesion(d) {
  const dec = d.decision || {};
  const ses = dec.session || {};

  if (dec.va_a_entrenar === false) return bloqueSinEntreno(d);
  // La bici se cuenta entera en `bloqueBici`, que es quien la sabe contar.
  if (esDiaDeBici(d)) return "";

  const dureza = ses.kind ? (DUREZA[ses.kind] || ses.kind) : "no se sabe";

  const sube = [];
  for (const p of [dec.progression, dec.progression_hiit]) {
    for (const c of (p && p.changes) || []) sube.push(c.text);
  }

  // La puerta cerrada se dice aunque no haya cambios, y sobre todo entonces:
  // «hoy no sube nada» y «hoy no puede subir nada, y este es el motivo» son la
  // misma pantalla en blanco con dos significados opuestos.
  const puerta =
    dec.progression && dec.progression.gate_open === false
      ? `<p class="tenue">${escapar(dec.progression.gate_reason || "progresión cerrada")}</p>`
      : "";

  return (
    `<h3>Lo que propondría</h3>` +
    `<p class="sesion">${escapar(ses.title || "—")}</p>` +
    `<p class="tenue">${escapar(dureza)}</p>` +
    bloqueLista("Sube hoy", sube) +
    puerta +
    bloqueLista("Cambios sobre la rutina", ses.changes) +
    bloqueLista("Hoy se quedan fuera", ses.dropped) +
    bloqueLista("Apuntes de la sesión", ses.notes)
  );
}

/* La bici. Sale entera del motor, incluidas las dos frases que explican de
 * dónde viene el punto de partida: `baseline_en_claro` está escrita para leerse
 * en el móvil y `baseline_why` para reconstruirlo después, y por eso aquí se
 * pinta la primera. */
function bloqueBici(d) {
  const b = (d.decision || {}).bike;
  // En un día de bici declarado, la bici es la sesión: va en afirmativo y se
  // dice aunque el motor no traiga recomendación, porque callarse dejaría la
  // tarjeta sin decir qué se hace hoy. Los apuntes de la sesión -qué sigue
  // tocando en el gimnasio- salen aquí, que es el único bloque que se pinta.
  const declarada = esDiaDeBici(d);
  const apuntes = declarada ? (((d.decision || {}).session || {}).notes || []) : [];
  if (!b) {
    if (!declarada) return "";
    return `<h3>Hoy sales en bici</h3>` + bloqueLista("Apuntes del día", apuntes);
  }

  if (!b.applies) {
    // `skip_visible` distingue «hoy aquí no se habla» de «hoy no he podido
    // calcularlo». Lo segundo se dice; lo primero no ocupa sitio.
    if (!b.skip_visible && !declarada) return "";
    return (
      `<h3>${declarada ? "Hoy sales en bici" : "Bici"}</h3>` +
      (b.skip_visible
        ? `<p>${escapar(b.skip_reason || "hoy no hay recomendación")}</p>`
        : "") +
      bloqueLista("Apuntes del día", apuntes)
    );
  }

  const rango =
    b.duration_min !== null && b.duration_min !== undefined &&
    b.duration_max !== null && b.duration_max !== undefined
      ? ` (${escapar(b.duration_min)}–${escapar(b.duration_max)} min)`
      : "";

  const recortes = [];
  for (const r of b.downgrades || []) recortes.push(r.why);

  return (
    `<h3>${declarada ? "Hoy sales en bici" : "Bici"}</h3>` +
    `<p class="sesion">${declarada ? "" : "Si sales hoy: "}` +
    `${escapar(b.label || b.level || "—")}${rango}</p>` +
    (b.detail ? `<p>${escapar(b.detail)}</p>` : "") +
    (b.baseline_en_claro ? `<p class="tenue">${escapar(b.baseline_en_claro)}</p>` : "") +
    bloqueLista("Rebajada porque", recortes) +
    bloqueLista("A tener en cuenta", b.notas) +
    bloqueLista("Apuntes del día", apuntes)
  );
}

/* «No estoy de acuerdo», en dos toques y no en uno.
 *
 * El primero abre el hueco del motivo; el segundo lo guarda. Podría ser uno
 * solo -caja de texto y botón siempre a la vista- y sería menos código, pero
 * dejaría una invitación permanente a discrepar encima de cada tarjeta. El
 * encargo dice lo contrario con todas las letras: «el objetivo no es forzar el
 * resultado que me apetece». Que haya que buscarlo es parte de que signifique
 * algo.
 *
 * El motivo NO es obligatorio. Un desacuerdo sin explicación sigue sirviendo
 * para las dos medidas que se cuentan -cuántas veces y en qué umbral- y
 * exigirlo dejaría sin registrar justo los días con prisa, que no son una
 * muestra al azar.
 *
 * `id` no lleva ninguno de estos elementos y no es descuido: `$()` busca por
 * `id` en el HTML, y un `id` escrito dentro de una plantilla no está en
 * `index.html`. Se buscan por clase desde la propia tarjeta, que además es lo
 * único que garantiza que el botón que se escucha sea el de ESTA tarjeta y no
 * el de una anterior que se quedó colgando.
 */
function pintarDesacuerdo(caja, previewId) {
  const hueco = caja.querySelector(".desacuerdo");
  if (!hueco) return;

  hueco.innerHTML =
    `<button type="button" class="abrir-desacuerdo">No estoy de acuerdo</button>`;

  hueco.querySelector(".abrir-desacuerdo").addEventListener("click", () => {
    hueco.innerHTML = `
      <label class="motivo">
        <span>¿Por qué? Una línea basta, y puede quedarse en blanco.</span>
        <textarea class="texto-desacuerdo" rows="2"
                  placeholder="Lo que tú sabes y el sistema no"></textarea>
      </label>
      <button type="button" class="guardar-desacuerdo">
        Guardar el desacuerdo
      </button>
      <p class="nota">Esto no cambia lo que se propone ni envía nada. Queda
         apuntado al lado de esta previsualización para poder mirarlo después.</p>`;

    const boton = hueco.querySelector(".guardar-desacuerdo");
    boton.addEventListener("click", async () => {
      boton.disabled = true;
      boton.textContent = "Guardando…";
      const texto = hueco.querySelector(".texto-desacuerdo").value.trim();
      try {
        const r = await fetch(API.desacuerdo(previewId), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // `disagreed` explícito y no dado por hecho por el servidor: el
          // modelo no le pone defecto justamente para que un cuerpo vacío no
          // apunte un desacuerdo que nadie declaró.
          body: JSON.stringify({ disagreed: true, reason: texto || null }),
        });
        const datos = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(escapar(JSON.stringify(datos.detail || r.status)));
        // Se pinta LO QUE HA QUEDADO GUARDADO, releído de la respuesta, no lo
        // que se acaba de escribir en la caja. El servidor normaliza -un motivo
        // de espacios se guarda como nada- y una confirmación que repitiera el
        // cuerpo enseñaría un motivo apuntado que en la base no está.
        hueco.innerHTML =
          `<p class="aviso tenue">Queda apuntado que no estás de acuerdo` +
          (datos.disagreement_reason
            ? `: «${escapar(datos.disagreement_reason)}»`
            : `, sin motivo escrito`) +
          `. Lo que decide sigue siendo lo que envíes.</p>`;
      } catch (e) {
        // Que no se haya guardado se dice, y se deja el botón otra vez
        // pulsable: un desacuerdo que se pierde no se nota hoy -la pantalla
        // parecería haberlo recogido- y sale como un cero limpio dentro de seis
        // meses, midiendo.
        boton.disabled = false;
        boton.textContent = "Guardar el desacuerdo";
        const mal = document.createElement("p");
        mal.className = "aviso mal";
        mal.textContent =
          `NO se ha guardado el desacuerdo (${e.message || e}). Inténtalo otra vez.`;
        hueco.appendChild(mal);
      }
    });
  });
}

function pintarPrevisualizacion(d) {
  const caja = $("previsualizacion");
  caja.hidden = false;
  caja.className = `previsualizacion semaforo-${escapar(d.light)}`;

  // `revision` viene del servidor resuelto, no de comparar `seq` con 1 aquí.
  // Es el aviso de que ésta no es la primera mirada del día, y eso es justo lo
  // que el encargo quiere poder ver: «no quiero que la segunda tape a la
  // primera, la diferencia entre ambas es el dato».
  const revision = d.revision === true
    ? `<p class="aviso ojo">No es la primera vez que miras hoy. Las anteriores
         siguen guardadas: la diferencia entre ellas es lo que se quiere
         medir.</p>`
    : "";

  caja.innerHTML =
    cabeceraDeLoQueSeHaTocado(d) +
    revision +
    `<h3><span class="punto"></span>` +
    `${escapar(NOMBRE_DE_LA_LUZ[d.light] || d.light || "—")}</h3>` +
    bloqueLista("Por qué", porQue(d)) +
    sinDatos(d) +
    // En un día de bici `bloqueSesion` y `bloqueEleccion` no pintan nada, y la
    // bici, que va la última los demás días, queda la primera sin moverla.
    bloqueSesion(d) +
    bloqueEleccion(d) +
    bloqueBici(d) +
    // El hueco del desacuerdo se pinta vacío aquí y lo rellena
    // `pintarDesacuerdo`, que necesita colgarle oyentes. Y solo si hay
    // `preview_id`: sin él no hay contra qué guardarlo, y un botón que no puede
    // hacer su trabajo es peor que no tenerlo.
    (d.preview_id ? `<div class="desacuerdo"></div>` : "");

  pintarEleccionSesion(caja);
  if (d.preview_id) pintarDesacuerdo(caja, d.preview_id);
}

/* Cuando la previsualización no sale.
 *
 * Pinta la MISMA cabecera de qué se ha tocado que la tarjeta buena, y ése es el
 * motivo de que el servidor mande el desglose también en el 409 y en el 502: un
 * error es exactamente el momento en que uno se pregunta si ha pasado algo, y
 * es el momento en que esta pantalla se callaba.
 *
 * El texto sale del `detail` en tres intentos y el último es el JSON crudo. Es
 * feo a propósito: un `[object Object]` en el sitio del motivo es el fallo que
 * `describirPendiente` ya se encontró una vez, y un aviso sin contenido manda a
 * buscar a ciegas. */
/* SUBIR DE DUREZA CON EL SEMÁFORO EN ROJO: la única pregunta que se hace.
 *
 * El motor no la contesta por su cuenta ni se traga la petición en silencio:
 * lanza `ConfirmacionNecesaria` y las dos rutas devuelven un 409 con las dos
 * sesiones puestas. Esta función pinta esa pregunta. El `sí` no vive en el
 * cliente: vuelve al servidor como `confirm_upgrade`, y allí se recalcula si
 * de verdad era una subida en rojo. Un cliente que mandara `true` siempre
 * -porque preguntar es incómodo- no podría marcar como forzadas las
 * anulaciones que no lo son; eso lo decide `SesionAnulada` mirando la LUZ.
 *
 * Se pinta en el hueco de la previsualización y no en un `confirm()` del
 * navegador a propósito: hace falta seguir viendo qué propone el sistema y por
 * qué mientras se decide, y un diálogo del navegador tapa justo eso.
 */
function pedirConfirmacionEnRojo(detalle) {
  const caja = $("previsualizacion");
  caja.hidden = false;
  caja.className = "previsualizacion semaforo-red";
  const pedida = NOMBRE_DEL_TIPO[detalle.pedida] || detalle.pedida;
  const propuesta = NOMBRE_DEL_TIPO[detalle.propuesta] || detalle.propuesta;

  caja.innerHTML =
    `<h3><span class="punto"></span>El semáforo está en ROJO</h3>` +
    `<p>El sistema propone <b>${escapar(propuesta)}</b> y has pedido ` +
    `<b>${escapar(pedida)}</b>. Subir de intensidad un día rojo es la única ` +
    `cosa que esta pantalla no hace sin preguntar.</p>` +
    `<p class="tenue">Si sigues, queda marcado aparte: no como un desacuerdo ` +
    `cualquiera, sino como una subida forzada en rojo. Es el dato que después ` +
    `dice cuántas veces se subió el día que no tocaba.</p>` +
    `<div class="confirmar-rojo">` +
      `<button type="button" class="confirmar-si">Sí, hago ${escapar(pedida)}</button>` +
      `<button type="button" class="confirmar-no">No, dejo lo que propone</button>` +
    `</div>`;

  caja.querySelector(".confirmar-si").addEventListener("click", () => {
    estado.confirmadaEnRojo = true;
    previsualizar();
  });
  caja.querySelector(".confirmar-no").addEventListener("click", () => {
    estado.sesionPedida = null;
    estado.confirmadaEnRojo = false;
    previsualizar();
  });
}

/* ¿Es este error la pregunta del rojo y no una avería? */
function esConfirmacionEnRojo(r, datos) {
  const d = datos && datos.detail !== undefined ? datos.detail : datos;
  return r.status === 409 && d && d.confirmacion_necesaria === true ? d : null;
}

function previsualizacionMal(titulo, detalle) {
  const caja = $("previsualizacion");
  caja.hidden = false;
  caja.className = "previsualizacion mal";

  const objeto = detalle && typeof detalle === "object" ? detalle : {};
  const texto = typeof detalle === "string"
    ? detalle
    : objeto.error || objeto.motivo || JSON.stringify(detalle);

  caja.innerHTML =
    cabeceraDeLoQueSeHaTocado(objeto) +
    `<h3>${escapar(titulo)}</h3>` +
    `<p>${escapar(texto)}</p>`;
}

async function previsualizar() {
  if (estado.enviando || estado.previsualizando) return;

  estado.previsualizando = true;
  revisar();
  $("previsualizar").textContent = "Mirando…";

  // Sin `comments`, al revés que el envío. No es un olvido: `/api/preview` no
  // guarda el comentario en ninguna parte -la fila apunta las respuestas, y el
  // comentario no es una respuesta-, así que mandarlo sería un campo que viaja
  // y se tira. La regla de esta casa es que no haya campos así.
  //
  // Tampoco `day`: lo mismo que hace `enviar()`. El día lo pone el reloj del
  // servidor, y que las dos rutas lo resuelvan igual es lo único que garantiza
  // que lo que se mira y lo que se manda sean del mismo día.
  const cuerpo = { ...estado.valores, ...loQuePido() };

  try {
    const r = await fetch(API.previsualizar, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuerpo),
    });
    const datos = await r.json().catch(() => ({}));
    const pregunta = esConfirmacionEnRojo(r, datos);
    if (pregunta) {
      pedirConfirmacionEnRojo(pregunta);
    } else if (!r.ok) {
      previsualizacionMal(
        `No se ha podido previsualizar (${r.status})`,
        datos.detail !== undefined ? datos.detail : datos,
      );
    } else {
      pintarPrevisualizacion(datos);
    }
  } catch (err) {
    // Sin servidor no hay nada que previsualizar, y aquí no hay borrador que
    // salvar ni nada que pueda haberse quedado a medias: la petición no salió.
    // Se dice así, y no como un fallo del sistema, porque llevan a mirar sitios
    // distintos.
    previsualizacionMal(
      "No se ha podido preguntar",
      `No se ha podido hablar con el servidor (${err.message || err}). ` +
      `Esto no dice nada sobre cómo está hoy: dice que no se ha podido ` +
      `preguntar.`,
    );
  } finally {
    estado.previsualizando = false;
    $("previsualizar").textContent = "Previsualizar otra vez";
    revisar();
    $("previsualizacion").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// `escapar` y `fechaLarga` estaban aquí duplicadas, letra por letra, con las de
// `comun.js`. Dos copias de un escape de HTML es de las peores cosas que se
// pueden duplicar: se arregla una el día que aparezca un carácter que se cuela y
// la otra se queda rota, sin que nada lo diga. Ahora vienen de `comun.js`, que
// `index.html` carga antes que esto.

// ---------------------------------------------------------------------------
// El estado del sistema
// ---------------------------------------------------------------------------

/* La marca de escritura a medias, en una línea que se pueda leer.
 *
 * `writes.pending_write` es el contenido ENTERO del fichero de marca -un objeto
 * con la rutina, la copia y la hora-, y aquí se metía tal cual dentro de una
 * plantilla de cadena. JavaScript convierte un objeto a texto llamando a su
 * `toString()`, y el de un objeto plano es la cadena literal `[object Object]`.
 * O sea que el aviso más grave de esta pantalla -«hay una rutina en Hevy en
 * estado desconocido»- salía sin decir CUÁL rutina ni CUÁNDO, que es todo lo que
 * uno necesita para ir a mirarla.
 *
 * Y el detalle de por qué duele: esto no revienta. No hay excepción, no hay
 * consola en rojo, no hay nada que se ponga a chillar. Sale un aviso con pinta
 * de aviso y con el dato sustituido por un texto que no significa nada. El mismo
 * patrón de siempre aquí: el valor que se lee no es el valor que se usa.
 *
 * La hora se recorta a `AAAA-MM-DD HH:MM` porque los segundos del intento no
 * ayudan a decidir nada, y el id de la rutina se deja entero: es lo que hay que
 * pegar en el comando de revertir.
 */
function describirPendiente(marca) {
  if (typeof marca === "string") return marca;
  if (!marca || typeof marca !== "object") return String(marca);
  const trozos = [];
  if (marca.routine_id) trozos.push(`rutina ${marca.routine_id}`);
  if (marca.started_at) {
    trozos.push(`empezada el ${String(marca.started_at).replace("T", " ").slice(0, 16)}`);
  }
  if (marca.note) trozos.push(String(marca.note));
  // Si la marca no trae ninguno de los campos esperados, se enseña el JSON
  // antes que una cadena vacía: un aviso sin contenido manda a buscar a ciegas.
  return trozos.length ? trozos.join(", ") : JSON.stringify(marca);
}

/* Lo que se avisa aquí NO se nota rellenando el formulario.
 *
 * Sin las claves de Hevy y Telegram el check-in se envía, se guarda y se
 * decide; lo que no ocurre es lo de después: ni se reescribe la rutina ni llega
 * el mensaje. Desde el móvil eso son dos cosas idénticas -no llega nada- y una
 * de ellas es una avería. Igual con el planificador parado: la aplicación sirve
 * el formulario, contesta 200 y no decide nunca.
 *
 * `/api/health` ya lo contaba todo. El healthcheck de Docker era su único
 * lector, y solo mira que el 200 llegue.
 *
 * El caso raro es `dry_run`, y por eso también se pinta: en seco el silencio es
 * DELIBERADO. Sin decirlo, un día sin mensaje de Telegram se lee como una
 * avería y se acaba tocando lo que no está roto.
 */
async function comprobarSalud() {
  let s;
  try {
    const r = await fetch(API.salud, { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    s = await r.json();
  } catch {
    // Callar a propósito: si no hay servidor, `arrancar()` ya lo dice arriba y
    // con todas las letras. Dos avisos de la misma causa se leen como dos
    // problemas distintos.
    return;
  }

  const avisos = [];

  const faltan = s.secrets_missing || [];
  if (faltan.length) {
    avisos.push({
      clase: "mal",
      titulo: "Faltan credenciales: el sistema no puede actuar.",
      cuerpo:
        "Lo que envíes se guarda y se decide, pero no se reescribirá la " +
        "rutina en Hevy ni llegará el mensaje.",
      lista: faltan,
    });
  }

  const plan = s.scheduler || {};
  const trabajos = Object.keys(plan.jobs || {});
  if (!plan.running || trabajos.length === 0) {
    avisos.push({
      clase: "mal",
      titulo: "El planificador no está en marcha: no va a decidir solo.",
      cuerpo: plan.error
        ? `El servidor dice: ${plan.error}`
        : "Nadie refrescará Garmin por la mañana ni decidirá si no hay " +
          "check-in. El formulario sigue funcionando a mano.",
    });
  }

  const reloj = s.clock || {};
  if (reloj.matches === false) {
    avisos.push({
      clase: "mal",
      titulo: "El reloj del servidor no va con las reglas.",
      cuerpo: reloj.error
        ? `El servidor dice: ${reloj.error}`
        : `Las reglas están en ${reloj.timezone} y el servidor va en ` +
          `${reloj.offset}. Un check-in de madrugada se guardará con la fecha ` +
          `de ayer, y mañana se decidirá como si no lo hubieras enviado.`,
    });
  }

  // El config que está decidiendo contra el que hay escrito en el disco.
  //
  // Faltaba, y es el que se cobró la pieza: el 13 de septiembre el contenedor
  // llevaba 30 horas decidiendo con un `config.yaml` que ya no existía en el
  // disco -editado esa misma tarde-, y ademas su código viejo rechazaba el
  // nuevo de plano. El servidor lo sabía y lo decía en `/api/health`; esta
  // pantalla no lo miraba, así que desde el móvil se veía un sistema perfecto.
  //
  // No dice el comando de recrear, y no es pereza: este mismo JS corre contra
  // dos despliegues con compose distintos, y dictar el comando del otro es peor
  // que no dictar ninguno -ya pasó una vez, y recreó el contenedor apuntando a
  // otro directorio de datos-.
  const conf = s.config_file || {};
  if (conf.in_sync === false) {
    avisos.push({
      clase: "mal",
      titulo: "El config del disco NO es el que está decidiendo.",
      cuerpo: conf.error
        ? `El servidor dice: ${conf.error}`
        : "Lo que hayas cambiado en config.yaml no se está aplicando. Hace " +
          "falta recrear el contenedor con el mismo compose con el que está " +
          "levantado.",
    });
  }

  // Una escritura empezada en Hevy y sin cerrar. Hay una copia previa guardada
  // esperando, y hasta que no se mire nadie sabe si la rutina de allí es la que
  // debería estar.
  const esc = s.writes || {};
  if (esc.pending_write) {
    avisos.push({
      clase: "mal",
      titulo: "Quedó una escritura a medias en Hevy.",
      cuerpo:
        `Se empezó a reescribir una rutina y no consta que terminara ` +
        `(${describirPendiente(esc.pending_write)}). Conviene mirar la rutina ` +
        `en Hevy antes de fiarse de ella.`,
    });
  }
  if (esc.pending_error) {
    avisos.push({
      clase: "mal",
      titulo: "No se ha podido comprobar si quedó una escritura a medias.",
      cuerpo: `El servidor dice: ${esc.pending_error}`,
    });
  }

  // LA RUTINA HUÉRFANA. El único aviso de esta pantalla que describe lo que hay
  // AHORA MISMO en otra aplicación, y el único que hay que leer antes de
  // entrenar y no después. Pasa cuando el check-in llega tarde, anula lo que el
  // respaldo de las 09:00 escribió, y la reversión no se puede hacer: en Hevy
  // queda una sesión que el sistema ya ha decidido que hoy no toca.
  //
  // El texto viene ENTERO del servidor y se pinta tal cual. Es deliberado: el
  // motivo se redactó para que sirva para actuar -«Abre Hevy y NO hagas Día 1:
  // hoy toca Recuperación»- y lleva los títulos reales de las dos rutinas, que
  // aquí no se conocen. Reescribirlo desde el JS sería inventarse una segunda
  // versión de lo mismo, más pobre y capaz de contradecir a Telegram.
  if (esc.stale_write) {
    avisos.push({
      clase: "mal",
      titulo: "En Hevy hay una rutina que hoy NO toca.",
      cuerpo: esc.stale_write,
    });
  }
  if (esc.stale_error) {
    avisos.push({
      clase: "mal",
      titulo: "No se ha podido comprobar qué quedó hoy en Hevy.",
      cuerpo: `El servidor dice: ${esc.stale_error}`,
    });
  }

  if (s.dry_run) {
    avisos.push({
      clase: "ojo",
      titulo: "Modo en seco.",
      cuerpo:
        "Se decide y se guarda todo, pero a propósito no se toca Hevy ni se " +
        "envía Telegram. Si hoy no llega mensaje, es esto y no una avería.",
    });
  }

  if (!avisos.length) return;

  const caja = $("salud");
  caja.innerHTML = avisos.map((a) => (
    `<p class="aviso ${a.clase}">` +
      `<strong>${escapar(a.titulo)}</strong>` +
      `${escapar(a.cuerpo)}` +
      (a.lista
        ? `<ul>${a.lista.map((x) => `<li>${escapar(x)}</li>`).join("")}</ul>`
        : "") +
    `</p>`
  )).join("");
  caja.hidden = false;
}

/* --------------------------------------------------------------------------
 * Los tres botones de comprobación
 * --------------------------------------------------------------------------
 * LO QUE SE PINTA ES LA CADENA ENTERA, NO UN SEMÁFORO. Un "Garmin: bien" no
 * sirve de nada el día que Garmin no está bien, porque lo siguiente que hace
 * falta saber es en qué eslabón se rompió, y eso el servidor ya lo manda paso a
 * paso. Reducirlo aquí a un color obligaría a entrar por SSH a buscar lo que ya
 * estaba en la respuesta.
 *
 * Los tres estados de cada paso son tres, no dos: bien, mal, y NO SE INTENTÓ.
 * El tercero existe porque una cadena que se corta en el primer eslabón deja
 * cuatro pasos sin hacer, y pintarlos en rojo haría pensar en cinco averías
 * cuando hay una avería y cuatro consecuencias.
 */

const ICONO = { true: "✓", false: "✗", null: "·" };

function pintarPrueba(d) {
  const caja = $("resultado-prueba");
  const pasos = (d.pasos || []).map((p) => {
    // `p.ok` puede ser `null`, y `String(null)` es "null": la tabla de arriba
    // tiene esa clave a propósito, para no acabar en `undefined`.
    const estado = p.ok === true ? "bien" : p.ok === false ? "mal" : "sin-hacer";
    return (
      `<li class="${estado}">` +
        `<span class="marca">${ICONO[String(p.ok)]}</span> ` +
        `<strong>${escapar(p.nombre)}</strong>: ${escapar(p.detalle)}` +
        (p.error ? `<code>${escapar(p.error)}</code>` : "") +
      `</li>`
    );
  }).join("");

  caja.innerHTML =
    `<p class="aviso ${d.ok ? "bien" : "mal"}">` +
      `<strong>${escapar(d.resumen || "")}</strong>` +
    `</p>` +
    `<ul class="pasos">${pasos}</ul>`;
  caja.hidden = false;
}

async function lanzarPrueba(boton) {
  const cual = boton.dataset.prueba;
  const url = API.probar[cual];
  if (!url) return;

  const antes = boton.textContent;
  // Se desactivan LOS TRES mientras tanto. Garmin limita los intentos, así que
  // dejar los otros pulsables invitaría justo a la ráfaga que provoca el 429.
  const todos = document.querySelectorAll("[data-prueba]");
  todos.forEach((b) => { b.disabled = true; });
  boton.textContent = "Comprobando…";

  try {
    const r = await fetch(url, { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || String(r.status));
    pintarPrueba(d);
  } catch (e) {
    // Que no conteste el servidor no es lo mismo que que conteste "mal", y se
    // dice cuál de las dos, porque llevan a mirar sitios distintos.
    const caja = $("resultado-prueba");
    caja.innerHTML =
      `<p class="aviso mal"><strong>No se pudo hacer la comprobación.</strong>` +
      `Esto no dice que ${escapar(cual)} esté mal: dice que no se ha podido ` +
      `preguntar. ${escapar(e.message || e)}</p>`;
    caja.hidden = false;
  } finally {
    todos.forEach((b) => { b.disabled = false; });
    boton.textContent = antes;
  }
}

document.querySelectorAll("[data-prueba]").forEach((b) => {
  b.addEventListener("click", () => lanzarPrueba(b));
});

$("formulario").addEventListener("submit", enviar);
$("comentarios").addEventListener("input", guardarBorrador);

// El de previsualizar se engancha aquí, al lado del envío y no dentro de
// `arrancar()`, porque los dos botones están escritos en `index.html` y existen
// desde el primer momento: no los pinta nadie, así que no hay que esperar a
// ninguna respuesta para poder escucharlos.
//
// `() => previsualizar()` y no `previsualizar` a secas. La diferencia es que un
// oyente recibe el evento como primer argumento, y `previsualizar()` no lo
// espera hoy; pasárselo funciona por casualidad y deja una trampa puesta para el
// día que la función crezca un parámetro -de esos que no revientan, sino que
// reciben un objeto `Event` donde esperaban una opción-.
$("previsualizar").addEventListener("click", () => previsualizar());
$("cambiar-respuestas").addEventListener("click", () => cambiarRespuestas());

// La barra de abajo, antes de pedir nada. No depende del servidor, así que se
// pinta ya: si el formulario no carga, desde aquí todavía se puede ir a mirar
// qué ha estado haciendo el motor, que es justo lo que se quiere saber cuando
// algo no va.
pintarNav("/");

arrancar();
// Aparte de `arrancar()` y sin `await`: son dos preguntas independientes. Un
// `/api/health` lento no debe retrasar el formulario, y un formulario que no
// carga no debe tapar el motivo por el que no carga.
comprobarSalud();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((e) => {
    // Sin service worker la aplicación funciona igual, solo que sin instalar.
    console.warn("service worker no registrado:", e);
  });
} else {
  // Este `else` no sobra. Servida por Umbrel la condición es FALSA -HTTP en la
  // red local no es contexto seguro, y ahí el navegador ni siquiera expone
  // `serviceWorker`-, así que sin esto no se registra nada Y no se dice nada:
  // el `.catch` de arriba no llega a correr. Queda un "añadir a pantalla de
  // inicio" que no instala nada y ni una pista de por qué.
  //
  // A la consola y no a la pantalla, a propósito: esto no afecta a decidir, y
  // un aviso permanente sobre algo que hoy no se va a arreglar solo enseña a
  // ignorar los avisos -que son justo los que sí hay que leer-.
  console.info(
    "service worker omitido: requiere contexto seguro (HTTPS o localhost). " +
    "La aplicación funciona igual, pero no arranca sin conexión."
  );
}
