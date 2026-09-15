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
  $("enviar").disabled = faltan.length > 0 || estado.enviando;
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
  if (estado.enviando) return;

  estado.enviando = true;
  revisar();
  $("enviar").textContent = "Enviando…";

  const cuerpo = { ...estado.valores };
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

/* El servidor puede contestar 200 con `decided: false`: el check-in se guardó y
 * la decisión falló. Eso NO es un éxito y no puede parecerlo, porque el usuario
 * cerraría el móvil convencido de que ya está y se enteraría al no llegarle el
 * mensaje. */
function pintarResultado(d) {
  const caja = $("resultado");
  caja.hidden = false;

  if (!d.decided) {
    caja.className = "resultado mal";
    caja.innerHTML = `
      <h2>Guardado, pero sin decidir</h2>
      <p>El check-in <strong>sí</strong> se ha guardado. Lo que ha fallado es la
         decisión del día, así que <strong>no</strong> se ha tocado la rutina de
         Hevy ni se ha enviado ningún mensaje.</p>
      <pre>${escapar(d.error || "sin detalle")}</pre>`;
    return;
  }

  const nombre = { green: "Verde", amber: "Ámbar", red: "Rojo" };
  caja.className = `resultado semaforo-${escapar(d.light)}`;
  caja.innerHTML = `
    <h2><span class="punto"></span>${escapar(nombre[d.light] || d.light)}</h2>
    <p class="sesion">${escapar(d.session || "")}</p>
    <dl>
      <dt>Hevy</dt><dd>${escapar(d.hevy || "—")}</dd>
      <dt>Telegram</dt><dd>${escapar(d.telegram || "—")}</dd>
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
  const p = document.createElement("p");
  p.className = `aviso ${clase}`;
  p.textContent = texto;
  $("formulario").prepend(p);
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
