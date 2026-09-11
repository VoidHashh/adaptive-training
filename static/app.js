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
 */

const API = {
  hoy: "/api/checkin/today",
  enviar: "/api/checkin",
  salud: "/api/health",
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
  valores: {},       // key -> número, SOLO de los contestados
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
  if (datos.comment_label) estado.etiquetaComentarios = datos.comment_label;
  $("etiqueta-comentarios").textContent = estado.etiquetaComentarios;

  pintarSliders();
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

function fijar(key, valor) {
  const fila = document.querySelector(`.slider[data-key="${CSS.escape(key)}"]`);
  if (!fila) return;   // un deslizador guardado que ya no está en el config
  fila.querySelector("input").value = String(valor);
  fila.classList.remove("sin-contestar", "descartado");
  estado.valores[key] = Number(valor);
  pintarValor(key);
}

function pintarValor(key) {
  const fila = document.querySelector(`.slider[data-key="${CSS.escape(key)}"]`);
  const out = fila.querySelector("output");
  if (fila.classList.contains("descartado")) out.textContent = "no entrené";
  else if (fila.classList.contains("sin-contestar")) out.textContent = "—";
  else out.textContent = String(estado.valores[key]);
}

/* Qué falta y por qué no se puede enviar todavía. Se dice con nombre y
 * apellidos: un botón gris sin explicación es un callejón sin salida. */
function revisar() {
  const faltan = estado.sliders
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
