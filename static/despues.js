/*
 * La pantalla de después de entrenar.
 *
 * QUÉ HACE Y QUÉ NO
 * -----------------
 * Pide `/api/sesion/hoy` al pulsar el botón -que lee Hevy en ese momento-,
 * pinta el formulario con los ejercicios de verdad de la sesión, y manda lo
 * contestado a `/api/sesion/feedback`. Nada más.
 *
 * No calcula: ni la resta entre la espalda de esta mañana y la de ahora, ni
 * cuántos ejercicios faltan en porcentaje, ni nada que el servidor no haya
 * mandado ya hecho. Es la regla de toda la PWA -está en la cabecera de
 * `comun.js`- y aquí tiene un motivo más: esa resta es EL dato de este
 * formulario, y un dato que sale distinto en el móvil y en el servidor es peor
 * que no tenerlo. La pantalla enseña «esta mañana: 1» al lado y la resta la
 * hace quien tiene tests.
 *
 * NO LLEVA NI UNA PREGUNTA ESCRITA
 * --------------------------------
 * Las escalas, los enunciados, las opciones de cada desplegable y las de las
 * dos elecciones llegan en la respuesta del servidor, que las saca de
 * `app/engine/feedback.py`. Es la misma regla que los deslizadores del
 * check-in: si estuvieran aquí, añadir una opción en Python la dejaría fuera de
 * la pantalla y nadie podría contestarla, sin que nada fallara.
 *
 * La primera versión de este fichero ya llevaba este párrafo -sin la palabra
 * «enunciados»- y a la vez los enunciados escritos más abajo. Se vio al abrir la
 * pantalla en el navegador, no en ningún test: la cabecera afirmaba algo que el
 * código de debajo desmentía. Es el mismo defecto que este repositorio persigue
 * en todas partes, dentro del fichero que se estaba escribiendo.
 *
 * LOS DESLIZADORES SON LOS DEL CHECK-IN, MARCADO INCLUIDO
 * -------------------------------------------------------
 * Misma estructura (`.slider`, `.sin-contestar`, `.valor`, `.pistas`) que
 * `pintarSliders` en `app.js`. No se comparte el código -viven en dos páginas-,
 * pero sí la forma, por dos motivos: se ven y se comportan igual, y un
 * deslizador sin tocar significa lo mismo en los dos sitios. NO SE ENVÍA. El
 * `value="5"` del `<input>` es donde se dibuja el pulgar, no una respuesta, y
 * mandarlo convertiría cada escala sin tocar en un cinco que nadie contestó.
 */

const estadoDespues = {
  datos: null,        // la respuesta de /api/sesion/hoy
  valores: {},        // escalas y elecciones CONTESTADAS; lo que no está, no se envía
  respuestas: {},     // key de ejercicio -> respuesta, o ausente
};

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

function arrancarDespues() {
  pintarNav("/despues.html");
  const f = $("fecha-despues");
  if (f) f.textContent = fechaLarga(new Date());
  $("cargar-sesion").addEventListener("click", cargarSesion);
  $("form-despues").addEventListener("submit", guardarDespues);
}

async function cargarSesion() {
  const boton = $("cargar-sesion");
  boton.disabled = true;
  $("cargando-despues").hidden = false;
  avisar(null);
  $("guardado-despues").hidden = true;
  try {
    const r = await fetch("/api/sesion/hoy");
    if (!r.ok) {
      avisar(`No se ha podido leer la sesión (${r.status}). Prueba otra vez.`);
      return;
    }
    const datos = await r.json();
    estadoDespues.datos = datos;
    pintarFormulario(datos);
  } catch (e) {
    // Sin red. No se pinta nada a medias: un formulario con los ejercicios de
    // una lectura vieja se contestaría sobre una sesión que ya no es esa.
    avisar("Sin conexión. El formulario necesita leer Hevy en el momento.");
  } finally {
    boton.disabled = false;
    $("cargando-despues").hidden = true;
  }
}

function avisar(texto) {
  const a = $("aviso-despues");
  a.hidden = !texto;
  a.textContent = texto || "";
}

// ---------------------------------------------------------------------------
// El formulario
// ---------------------------------------------------------------------------

function pintarFormulario(d) {
  const form = $("form-despues");

  if (!d.hay_sesion) {
    form.hidden = true;
    // El motivo del cliente de Hevy va delante si lo hay: «no hay entreno» y
    // «no se ha podido preguntar» se leen igual en una línea y piden cosas
    // opuestas -esperar o arreglar las claves-.
    avisar(
      d.motivo_sin_hevy
        ? `No se ha podido leer Hevy: ${d.motivo_sin_hevy}`
        : "Todavía no hay ningún entreno de hoy en Hevy. Cuando lo guardes allí, vuelve a pulsar.",
    );
    return;
  }

  const ejercicios = d.ejercicios || [];
  const faltan = ejercicios.filter((e) => e.estado === "falta");
  const hechos = ejercicios.filter((e) => e.estado === "hecho");

  // Lo ya guardado se precarga: el formulario se puede rectificar, y abrirlo
  // otra vez para añadir una cosa no puede obligar a contestar las demás.
  estadoDespues.valores = {};
  estadoDespues.respuestas = {};
  const g = d.guardado || {};
  for (const e of d.escalas || []) {
    if (g[e.key] !== null && g[e.key] !== undefined) estadoDespues.valores[e.key] = g[e.key];
  }
  for (const k of ["cantidad", "tecnica", "mas_costoso"]) {
    if (g[k]) estadoDespues.valores[k] = g[k];
  }
  for (const e of ejercicios) {
    if (e.respuesta) estadoDespues.respuestas[e.key] = e.respuesta;
  }

  $("resumen-sesion").textContent = resumen(hechos.length, faltan.length);
  const tx = d.textos || {};
  pintarFaltan(faltan, d.respuestas.falta || {}, tx.falta);
  pintarHechos(hechos, d.respuestas.hecho || {}, tx.sin_problema);
  pintarEscalas(d.escalas || []);
  pintarElecciones(d);
  pintarCostoso(hechos, tx.mas_costoso, tx.ninguno);
  $("nota-despues").value = g.nota || "";

  form.hidden = false;
}

function resumen(nHechos, nFaltan) {
  const hechos = `${nHechos} ${nHechos === 1 ? "ejercicio hecho" : "ejercicios hechos"}`;
  if (!nFaltan) return hechos;
  return `${hechos} · ${nFaltan} sin hacer`;
}

/* Un desplegable de respuestas. La opción vacía va siempre la primera y
 * significa «no contesto», que para lo hecho es «sin problema». */
function desplegable(key, opciones, vacia) {
  const actual = estadoDespues.respuestas[key] || "";
  const ops = [`<option value="">${escapar(vacia)}</option>`].concat(
    Object.entries(opciones).map(
      ([v, etiqueta]) =>
        `<option value="${escapar(v)}"${v === actual ? " selected" : ""}>${escapar(etiqueta)}</option>`,
    ),
  );
  return `<select class="respuesta-ejercicio" data-key="${escapar(key)}">${ops.join("")}</select>`;
}

function engancharDesplegables(cont) {
  for (const s of cont.querySelectorAll(".respuesta-ejercicio")) {
    s.addEventListener("change", () => {
      if (s.value) estadoDespues.respuestas[s.dataset.key] = s.value;
      else delete estadoDespues.respuestas[s.dataset.key];
    });
  }
}

function pintarFaltan(faltan, opciones, vacia) {
  const cont = $("faltan-despues");
  if (!faltan.length) {
    cont.innerHTML = "";
    return;
  }
  cont.innerHTML = `
    <div class="bloque-faltan">
      <p class="titulo-faltan">${faltan.length === 1 ? "Falta 1 del plan" : `Faltan ${faltan.length} del plan`}</p>
      ${faltan.map((e) => `
        <div class="ejercicio-despues">
          <p class="nombre-ejercicio">${escapar(e.name)}</p>
          ${desplegable(e.key, opciones, vacia)}
        </div>`).join("")}
    </div>`;
  engancharDesplegables(cont);
}

/* Lo que fue bien, plegado con un `<details>` nativo. No hace falta JavaScript
 * para abrirlo y cerrarlo, se lee bien con lector de pantalla, y si algún día
 * este fichero no carga, el contenido sigue ahí. Se abre solo si ya había algo
 * marcado: plegar un problema ya contado lo escondería. */
function pintarHechos(hechos, opciones, vacia) {
  const cont = $("hechos-despues");
  if (!hechos.length) {
    cont.innerHTML = "";
    return;
  }
  const algunoMarcado = hechos.some((e) => estadoDespues.respuestas[e.key]);
  const titulo = hechos.length === 1 ? "El otro, bien" : `Los otros ${hechos.length}, bien`;
  cont.innerHTML = `
    <details class="bloque-hechos"${algunoMarcado ? " open" : ""}>
      <summary>${escapar(titulo)}<span class="pista-hechos">toca si alguno dio guerra</span></summary>
      ${hechos.map((e) => `
        <div class="ejercicio-despues">
          <p class="nombre-ejercicio">${escapar(e.name)}</p>
          ${desplegable(e.key, opciones, vacia)}
        </div>`).join("")}
    </details>`;
  engancharDesplegables(cont);
}

function pintarEscalas(escalas) {
  const cont = $("escalas-despues");
  cont.innerHTML = "";
  for (const e of escalas) {
    const fila = document.createElement("div");
    const contestada = estadoDespues.valores[e.key] !== undefined;
    fila.className = contestada ? "slider" : "slider sin-contestar";
    fila.dataset.key = e.key;
    const id = `desp-${escapar(e.key)}`;
    const manana = e.manana === null || e.manana === undefined
      ? ""
      : `<span class="manana">esta mañana: ${escapar(e.manana)}</span>`;
    fila.innerHTML = `
      <div class="linea">
        <label for="${id}">${escapar(e.label)}${manana}</label>
        <output class="valor">${contestada ? escapar(estadoDespues.valores[e.key]) : "—"}</output>
      </div>
      <input type="range" id="${id}" min="0" max="10" step="1"
             value="${contestada ? escapar(estadoDespues.valores[e.key]) : "5"}"
             aria-label="${escapar(e.label)}">
      <div class="pistas"><span>${escapar(e.hint_low)}</span><span>${escapar(e.hint_high)}</span></div>`;
    const input = fila.querySelector("input");
    // `input` y no `change`, por lo mismo que en el check-in: en táctil `change`
    // no salta hasta soltar el dedo y el número se quedaría en «—».
    input.addEventListener("input", () => {
      estadoDespues.valores[e.key] = Number(input.value);
      fila.classList.remove("sin-contestar");
      fila.querySelector(".valor").textContent = input.value;
    });
    cont.appendChild(fila);
  }
}

function pintarElecciones(d) {
  const cont = $("elecciones-despues");
  const grupos = (d.elecciones || []).map((e) => [e.key, e.enunciado, e.opciones || {}]);
  cont.innerHTML = grupos.map(([key, enunciado, opciones]) => `
    <div class="pregunta${estadoDespues.valores[key] ? "" : " sin-contestar"}" data-key="${escapar(key)}">
      <p class="enunciado" id="enun-${escapar(key)}">${escapar(enunciado)}</p>
      <div class="eleccion" role="group" aria-labelledby="enun-${escapar(key)}">
        ${Object.entries(opciones).map(([v, etiqueta]) => {
          const puesto = estadoDespues.valores[key] === v;
          return `<button type="button" data-valor="${escapar(v)}" aria-pressed="${puesto}">${escapar(etiqueta)}</button>`;
        }).join("")}
      </div>
    </div>`).join("");

  for (const bloque of cont.querySelectorAll(".pregunta")) {
    const key = bloque.dataset.key;
    for (const b of bloque.querySelectorAll("[data-valor]")) {
      b.addEventListener("click", () => {
        // Pulsar el ya elegido lo DESMARCA. Sin esto no habría forma de volver
        // a «sin contestar» una vez tocado, y un toque por error se quedaría
        // guardado como respuesta.
        const ya = estadoDespues.valores[key] === b.dataset.valor;
        if (ya) delete estadoDespues.valores[key];
        else estadoDespues.valores[key] = b.dataset.valor;
        for (const otro of bloque.querySelectorAll("[data-valor]")) {
          otro.setAttribute("aria-pressed", String(!ya && otro === b));
        }
        bloque.classList.toggle("sin-contestar", ya);
      });
    }
  }
}

function pintarCostoso(hechos, enunciado, ninguno) {
  const cont = $("costoso-despues");
  if (!hechos.length) {
    cont.innerHTML = "";
    return;
  }
  const actual = estadoDespues.valores.mas_costoso || "";
  cont.innerHTML = `
    <label class="costoso">
      <span class="enunciado">${escapar(enunciado)}</span>
      <select id="mas-costoso">
        <option value="">${escapar(ninguno)}</option>
        ${hechos.map((e) => `<option value="${escapar(e.key)}"${e.key === actual ? " selected" : ""}>${escapar(e.name)}</option>`).join("")}
      </select>
    </label>`;
  // Se busca dentro de su contenedor y no por `id`: lo acaba de crear el
  // `innerHTML` de arriba, y el arnés de tests -igual que un DOM estricto- solo
  // encuentra por `id` lo que ya venía en el HTML.
  const sel = cont.querySelector("select");
  sel.addEventListener("change", () => {
    if (sel.value) estadoDespues.valores.mas_costoso = sel.value;
    else delete estadoDespues.valores.mas_costoso;
  });
}

// ---------------------------------------------------------------------------
// El envío
// ---------------------------------------------------------------------------

/* El cuerpo exacto del POST. Aparte para que el arnés lo pueda leer sin mandar
 * nada: lo que hay que demostrar es que una escala sin tocar NO viaja. */
function cuerpoDespues() {
  const d = estadoDespues.datos;
  const v = estadoDespues.valores;
  const cuerpo = { day: d.day };
  for (const e of d.escalas || []) {
    if (v[e.key] !== undefined) cuerpo[e.key] = v[e.key];
  }
  for (const k of ["cantidad", "tecnica", "mas_costoso"]) {
    if (v[k] !== undefined) cuerpo[k] = v[k];
  }
  const nota = $("nota-despues").value.trim();
  if (nota) cuerpo.nota = nota;
  // `??` y no `||`. Con `||` una respuesta vacía se convertía aquí en `null`, y
  // eso tapaba en silencio lo mismo que ya limpia `engancharDesplegables` al
  // volver a «sin problema»: la misma limpieza en dos sitios, y el banco de
  // mutaciones no podía distinguir si la de allí funcionaba. Ahora el estado es
  // la única fuente, y si alguna vez guarda un `""` viajará como `""` y el
  // servidor lo rechazará con su motivo, en vez de pasar desapercibido.
  cuerpo.ejercicios = (d.ejercicios || []).map((e) => ({
    key: e.key,
    name: e.name,
    estado: e.estado,
    respuesta: estadoDespues.respuestas[e.key] ?? null,
  }));
  return cuerpo;
}

async function guardarDespues(ev) {
  ev.preventDefault();
  const boton = $("guardar-despues");
  boton.disabled = true;
  avisar(null);
  try {
    const r = await fetch("/api/sesion/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuerpoDespues()),
    });
    if (!r.ok) {
      // El 422 trae el motivo escrito por el validador; se enseña tal cual,
      // porque es la única pista de qué opción ha dejado de existir.
      let detalle = `error ${r.status}`;
      try {
        const j = await r.json();
        if (j && j.detail) detalle = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch (_e) { /* el cuerpo no era JSON: se queda el código */ }
      avisar(`No se ha guardado: ${detalle}`);
      return;
    }
    const g = $("guardado-despues");
    g.innerHTML = `<p class="hecho-guardado">Guardado. Puedes volver a abrirlo y cambiar lo que quieras.</p>`;
    g.hidden = false;
  } catch (_e) {
    avisar("Sin conexión: no se ha guardado. Lo que has contestado sigue en pantalla.");
  } finally {
    boton.disabled = false;
  }
}

arrancarDespues();
