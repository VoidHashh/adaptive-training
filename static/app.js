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
        `(${esc.pending_write}). Conviene mirar la rutina en Hevy antes de ` +
        `fiarse de ella.`,
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
