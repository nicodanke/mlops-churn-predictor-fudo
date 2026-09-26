/* Dashboard de riesgo de churn.
 *
 * Consume la API de solo lectura que sirve los resultados del scoring batch.
 * La URL de la API se resuelve, en orden: ?api= en la query string, la variable
 * window.CHURN_API_URL (que inyecta config.js), o localhost:8000.
 *
 * En Cloud Run el dashboard lo sirve la propia API, detras de Identity-Aware Proxy:
 * config.js apunta al mismo origen y la sesion de Google viaja sola en cada fetch.
 */

const API = (() => {
  const fromQuery = new URLSearchParams(location.search).get("api");
  if (fromQuery) return fromQuery.replace(/\/$/, "");
  if (window.CHURN_API_URL) return String(window.CHURN_API_URL).replace(/\/$/, "");
  return "http://localhost:8000";
})();

const RISK_LABELS = { alto: "Alto", medio: "Medio", bajo: "Bajo", no_churn: "Sin riesgo" };
const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
               "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];

const RISK_COLORS = {
  alto: "var(--alto)", medio: "var(--medio)", bajo: "var(--bajo)", no_churn: "var(--ok)",
};

const state = {
  view: "riesgo",
  eda: null,
  periodo: null,
  currency: "USD",
  risk: ["alto", "medio", "bajo"],
  search: "",
  sortBy: "revenue_at_risk",
  ascending: false,
  // null = todas · true = solo estacionales · false = excluirlas
  seasonal: null,
  page: 1,
  size: 25,
  pages: 1,
};

const $ = (id) => document.getElementById(id);

/* ---------------------------------------------------------------- helpers */

async function api(path, params = {}) {
  const url = new URL(API + path);
  if (state.periodo) url.searchParams.set("periodo", state.periodo);
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => url.searchParams.append(key, v));
    else url.searchParams.set(key, value);
  }
  let response;
  try {
    response = await fetch(url);
  } catch (error) {
    // Detras de IAP una sesion vencida no devuelve 401: redirige al login de Google, y
    // el navegador corta ese redirect dentro de un fetch. Recargar vuelve a pedir login.
    throw new Error(`${error.message}. Si la sesión venció, recargá la página.`);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

const money = (n) =>
  new Intl.NumberFormat("es-AR", { maximumFractionDigits: 0 }).format(Math.round(n || 0));
const pct = (n, digits = 0) => `${(100 * (n || 0)).toFixed(digits)}%`;
const num = (n) => new Intl.NumberFormat("es-AR").format(n || 0);

function periodLabel(periodo) {
  const s = String(periodo);
  const months = ["ene", "feb", "mar", "abr", "may", "jun",
                  "jul", "ago", "sep", "oct", "nov", "dic"];
  return `${months[parseInt(s.slice(4, 6), 10) - 1]} ${s.slice(0, 4)}`;
}

/** Color del mapa de calor: un percentil alto en una feature que empuja el riesgo
 *  se pinta rojo; una que lo contiene, verde. */
function heatColor(contribution) {
  return contribution.direction === "aumenta" ? "var(--alto)" : "var(--ok)";
}

function showBanner(message, kind = "warn") {
  const el = $("banner");
  el.textContent = message;
  el.classList.toggle("hidden", !message);
}

/* ------------------------------------------------------------------ carga */

/** Muestra con qué cuenta se entró. Solo hay sesión detrás de IAP (Cloud Run). */
async function loadSession() {
  try {
    const me = await api("/api/v1/me");
    if (!me.authenticated) return;
    $("session-email").textContent = me.email;
    $("session").classList.remove("hidden");
  } catch {
    // Sin sesión que mostrar: el resto del dashboard funciona igual.
  }
}

async function boot() {
  loadSession();
  wireTabs();
  wireChartResize();
  try {
    const periodos = await api("/api/v1/periodos");
    if (!periodos.length) {
      showBanner("Todavía no hay predicciones generadas. Corré `make score` para producir el primer batch.");
      $("accounts-body").innerHTML = `<tr><td colspan="6" class="empty">Sin datos</td></tr>`;
      return;
    }
    state.periodo = periodos[0];

    const select = $("period-select");
    select.innerHTML = periodos
      .map((p) => `<option value="${p}">${periodLabel(p)}</option>`)
      .join("");
    select.value = state.periodo;
    select.onchange = () => {
      state.periodo = Number(select.value);
      state.page = 1;
      loadAll();
    };

    renderRiskFilters();
    wireControls();
    await loadAll();
  } catch (error) {
    showBanner(`No se pudo conectar con la API en ${API} — ${error.message}`);
  }
}

async function loadAll() {
  await Promise.all([loadSummary(), loadHistogram(), loadImportance()]);
  await loadAccounts();
}

/* --------------------------------------------------------------- resumen */

async function loadSummary() {
  const data = await api("/api/v1/summary");
  state.currency = data.currency;

  $("batch-meta").innerHTML = `
    <div>Predicción de bajas de <strong>${periodLabel(data.periodo_prediccion)}</strong></div>
    <div>${num(data.n_accounts)} cuentas activas · generado ${data.generated_at.slice(0, 10)}</div>`;

  if (!data.pricing_loaded) {
    showBanner(
      "La lista de precios está sin cargar (config/pricing.yaml): el revenue en riesgo se muestra en 0. " +
      "Completá los precios por plan y módulo y volvé a correr el scoring."
    );
  } else {
    showBanner("");
  }

  const by = Object.fromEntries(data.summary.map((r) => [r.risk_category, r]));
  const enRiesgo = ["alto", "medio", "bajo"].reduce((acc, k) => acc + (by[k]?.cuentas || 0), 0);
  const revenueEnRiesgo = ["alto", "medio", "bajo"]
    .reduce((acc, k) => acc + (by[k]?.revenue_en_riesgo || 0), 0);

  const cards = [
    {
      label: "Cuentas en riesgo", value: num(enRiesgo), accent: "var(--brand)",
      sub: `${pct(enRiesgo / data.n_accounts, 1)} de la base · umbral ${pct(data.decision_threshold)}`,
    },
    {
      label: "Riesgo alto", value: num(by.alto?.cuentas || 0), accent: "var(--alto)",
      sub: `${money(by.alto?.revenue_en_riesgo)} ${data.currency}/mes en juego`,
    },
    {
      label: "Riesgo medio", value: num(by.medio?.cuentas || 0), accent: "var(--medio)",
      sub: `${money(by.medio?.revenue_en_riesgo)} ${data.currency}/mes en juego`,
    },
    {
      label: "Revenue en riesgo", value: `${money(revenueEnRiesgo)}`, accent: "var(--brand)",
      sub: `${data.currency} por mes, sobre cuentas señaladas`,
    },
  ];

  $("kpis").innerHTML = cards
    .map((c) => `
      <div class="kpi" style="--accent:${c.accent}">
        <div class="kpi-label">${c.label}</div>
        <div class="kpi-value">${c.value}</div>
        <div class="kpi-sub">${c.sub}</div>
      </div>`)
    .join("");
}

/* ----------------------------------------------------------- histograma */

async function loadHistogram() {
  const data = await api("/api/v1/distribution", { bins: 24 });
  const max = Math.max(...data.bins.map((b) => b.cuentas), 1);

  // Raiz cuadrada: la primera barra concentra el 95% de la base y en escala lineal
  // aplasta a las demas hasta hacerlas invisibles, que son justo las que interesan.
  const scale = (n) => (Math.sqrt(n) / Math.sqrt(max)) * 100;

  $("histogram").innerHTML = data.bins
    .map((b) => {
      const height = b.cuentas === 0 ? 1 : Math.max(3, scale(b.cuentas));
      const over = b.desde >= data.threshold ? " over" : "";
      const tip = `${num(b.cuentas)} cuentas · ${pct(b.desde)}–${pct(b.hasta)}`;
      return `<div class="hbar${over}" style="height:${height}%" data-tip="${tip}"></div>`;
    })
    .join("");

  const overThreshold = data.bins
    .filter((b) => b.desde >= data.threshold)
    .reduce((acc, b) => acc + b.cuentas, 0);
  $("histogram-legend").innerHTML =
    `En rojo, las ${num(overThreshold)} cuentas por encima del umbral de decisión ` +
    `(${pct(data.threshold, 1)}). Altura en escala de raíz cuadrada: en escala lineal ` +
    `la primera barra aplastaría al resto.`;
}

/* -------------------------------------------------- importancia global */

async function loadImportance() {
  const rows = (await api("/api/v1/importance")).slice(0, 10);
  const max = Math.max(...rows.map((r) => r.mean_abs_shap), 0.0001);

  $("global-importance").innerHTML = rows
    .map((r) => `
      <div class="imp-row" title="${r.label} · ${r.group}">
        <div class="imp-label">${r.label}</div>
        <div class="imp-track"><div class="imp-fill" style="width:${(r.mean_abs_shap / max) * 100}%"></div></div>
      </div>`)
    .join("");
}

/* ====================================================================== */
/* Vista "Uso de la base": estadistica descriptiva que deja `churn eda`.    */
/* ====================================================================== */

/** El ancho del SVG se fija al dibujar, asi que hay que rehacerlo al cambiar el viewport. */
function wireChartResize() {
  let timer;
  window.addEventListener("resize", () => {
    if (!state.eda || state.view !== "uso") return;
    clearTimeout(timer);
    timer = setTimeout(() => { renderBaseChart(); renderChurnChart(); }, 160);
  });
}

function wireTabs() {
  $("tabs").querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => showView(tab.dataset.view);
  });
  // La pestaña viaja en el hash para poder mandar el link de una de las dos vistas.
  window.addEventListener("hashchange", () => showView(location.hash.slice(1)));
  showView(location.hash.slice(1));
}

function showView(view) {
  state.view = view === "uso" ? "uso" : "riesgo";
  $("tabs").querySelectorAll(".tab")
    .forEach((t) => t.classList.toggle("on", t.dataset.view === state.view));
  $("view-riesgo").classList.toggle("hidden", state.view !== "riesgo");
  $("view-uso").classList.toggle("hidden", state.view !== "uso");
  // El selector de periodo solo aplica al batch de predicciones; la vista de uso
  // describe el panel entero y no cambia con el mes elegido.
  $("period-field").classList.toggle("hidden", state.view !== "riesgo");
  if (location.hash.slice(1) !== state.view) location.hash = state.view;
  if (state.view === "uso") loadEda();
}

async function loadEda() {
  if (state.eda) return;  // el reporte es uno solo, no depende del periodo elegido
  try {
    state.eda = await api("/api/v1/eda");
  } catch (error) {
    $("eda-kpis").innerHTML =
      `<p class="empty">No hay estadística de la base todavía: generala con <code>churn eda</code>. (${escapeHtml(error.message)})</p>`;
    return;
  }
  renderEdaKpis();
  renderBaseChart();
  renderChurnChart();
  renderAdopcion();
  renderSenales();
  renderCortes();
}

function renderEdaKpis() {
  const { panel, base } = state.eda;
  const cards = [
    {
      label: "Cuentas activas", value: num(base.cuentas_activas_ultimo), accent: "var(--chart-1)",
      sub: `+${base.crecimiento_total.toFixed(1)}% desde ${periodLabel(panel.periodo_desde)} · ${base.crecimiento_mensual.toFixed(2)}% mensual`,
    },
    {
      label: "Churn base", value: `${base.churn_rate_promedio.toFixed(2)}%`, accent: "var(--chart-2)",
      sub: `mensual, entre ${base.churn_rate_min.toFixed(2)}% y ${base.churn_rate_max.toFixed(2)}% · es el número a batir`,
    },
    {
      label: "Retención anual", value: `${base.retencion_anual.toFixed(0)}%`, accent: "var(--chart-1)",
      sub: `vida media de una cuenta: ${base.vida_media_meses.toFixed(0)} meses`,
    },
    {
      label: "Altas acumuladas", value: num(base.altas_totales), accent: "var(--chart-1)",
      sub: `contra ${num(base.bajas_confirmadas_totales)} bajas confirmadas en ${panel.periodos} meses`,
    },
  ];
  $("eda-kpis").innerHTML = cards
    .map((c) => `
      <div class="kpi" style="--accent:${c.accent}">
        <div class="kpi-label">${c.label}</div>
        <div class="kpi-value">${c.value}</div>
        <div class="kpi-sub">${c.sub}</div>
      </div>`)
    .join("");
}

/* ------------------------------------------------------ graficos SVG */

/* Dos graficos separados y no uno con dos ejes: cuentas activas y churn rate se miden
 * en unidades distintas, y superponerlas en una sola caja deja que la escala elegida
 * decida que historia se cuenta. */

const CHART_PAD = { top: 16, right: 40, bottom: 22, left: 40 };
const CHART_H = 172;
// Padding horizontal de .chart en el CSS. El SVG se dibuja con las unidades del
// viewBox iguales a pixeles, asi que 2px de trazo son 2px en pantalla.
const CHART_PAD_X = 20;

/** Escalas del area de dibujo.
 *
 * `band` cambia como se reparte el eje x. Una linea une puntos, asi que el primero y el
 * ultimo van pegados a los bordes; una barra ocupa una franja, asi que su centro cae en
 * el medio de la franja. Usar la escala de linea para barras desalinea las etiquetas del
 * eje y el crosshair respecto de la barra que señalan — medio ancho de barra de error.
 */
function chartFrame(el, n, maxValue, band = false) {
  // clientWidth incluye el padding de .chart (20px por lado), que no es area de dibujo.
  // Si el ancho da 0 la vista todavia esta oculta: se usa un ancho razonable y el
  // listener de resize vuelve a dibujar cuando se muestre.
  const width = Math.max((el.clientWidth || 600) - 2 * CHART_PAD_X, 260);
  const inner = {
    w: width - CHART_PAD.left - CHART_PAD.right,
    h: CHART_H - CHART_PAD.top - CHART_PAD.bottom,
  };
  // Escala desde cero: en una serie de conteos o de tasas, recortar la base exagera
  // visualmente cualquier variacion.
  const top = maxValue * 1.12 || 1;
  const paso = inner.w / n;
  return {
    width,
    inner,
    paso,
    x: band
      ? (i) => CHART_PAD.left + i * paso + paso / 2
      : (i) => CHART_PAD.left + (n === 1 ? inner.w / 2 : (i / (n - 1)) * inner.w),
    y: (v) => CHART_PAD.top + inner.h - (v / top) * inner.h,
    top,
  };
}

function chartTip(el) {
  let tip = el.querySelector(".chart-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "chart-tip";
    el.appendChild(tip);
  }
  return tip;
}

/** Crosshair + tooltip: el grafico se lee con el mouse, no solo de un vistazo. */
function wireHover(el, svg, puntos, scale, describe) {
  const tip = chartTip(el);
  const cursor = svg.querySelector(".cursor");
  const marker = svg.querySelector(".marker");
  const hit = svg.querySelector(".hit");

  hit.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * scale.width;
    let i = 0, best = Infinity;
    puntos.forEach((p, k) => {
      const d = Math.abs(scale.x(k) - px);
      if (d < best) { best = d; i = k; }
    });
    const cx = scale.x(i), cy = scale.y(puntos[i].v);
    cursor.setAttribute("x1", cx); cursor.setAttribute("x2", cx);
    cursor.style.opacity = 1;
    marker.setAttribute("cx", cx); marker.setAttribute("cy", cy);
    marker.style.opacity = 1;
    tip.innerHTML = describe(puntos[i]);
    // El viewBox mide lo mismo que la caja de dibujo, asi que la coordenada del SVG
    // se traslada al contenedor sumando su padding.
    tip.style.left = `${CHART_PAD_X + cx}px`;
    tip.style.top = `${cy - 8}px`;
    tip.style.opacity = 1;
  });
  hit.addEventListener("mouseleave", () => {
    tip.style.opacity = 0;
    cursor.style.opacity = 0;
    marker.style.opacity = 0;
  });
}

// Ancho aproximado de una etiqueta "ene 2025" mas su aire. Por debajo de esto dos
// etiquetas contiguas se tocan.
const TICK_MIN_PX = 62;

/** Etiquetas del eje x: solo las que entran sin pisarse.
 *
 * El ultimo periodo siempre lleva etiqueta — es el que se busca al mirar el grafico —
 * y si el tick regular anterior le queda encima, gana el ultimo y el otro se cae.
 */
function xTicks(puntos, scale) {
  const cada = Math.max(1, Math.ceil(puntos.length / 7));
  const ultimo = puntos.length - 1;
  const indices = puntos.map((_, i) => i).filter((i) => i % cada === 0 && i !== ultimo);
  while (indices.length && scale.x(ultimo) - scale.x(indices[indices.length - 1]) < TICK_MIN_PX) {
    indices.pop();
  }
  indices.push(ultimo);

  return indices
    .map((i) =>
      `<text class="axis-label" x="${scale.x(i).toFixed(1)}" y="${CHART_H - 6}" text-anchor="middle">${periodLabel(puntos[i].periodo)}</text>`)
    .join("");
}

function renderBaseChart() {
  const el = $("chart-base");
  const puntos = state.eda.base.serie.map((r) => ({ ...r, v: r.cuentas_activas }));
  const max = Math.max(...puntos.map((p) => p.v));
  const scale = chartFrame(el, puntos.length, max);

  const linea = puntos.map((p, i) => `${i ? "L" : "M"}${scale.x(i).toFixed(1)},${scale.y(p.v).toFixed(1)}`).join(" ");
  const base = CHART_PAD.top + scale.inner.h;
  const area = `${linea} L${scale.x(puntos.length - 1).toFixed(1)},${base} L${scale.x(0).toFixed(1)},${base} Z`;

  const ultimo = puntos[puntos.length - 1];
  const grid = [0, 0.5, 1].map((f) => {
    const y = scale.y(scale.top * f);
    return `<line class="grid-line" x1="${CHART_PAD.left}" x2="${scale.width - CHART_PAD.right}" y1="${y}" y2="${y}"/>
            <text class="axis-label" x="${CHART_PAD.left - 6}" y="${y + 3}" text-anchor="end">${num(Math.round(scale.top * f))}</text>`;
  }).join("");

  el.innerHTML = `
    <svg viewBox="0 0 ${scale.width} ${CHART_H}" role="img"
         aria-label="Cuentas activas por mes, de ${num(puntos[0].v)} en ${periodLabel(puntos[0].periodo)} a ${num(ultimo.v)} en ${periodLabel(ultimo.periodo)}">
      ${grid}
      <path class="serie-area" d="${area}"/>
      <path class="serie-line" d="${linea}"/>
      <circle class="punto-final" cx="${scale.x(puntos.length - 1)}" cy="${scale.y(ultimo.v)}" r="4"/>
      <text class="valor-final" x="${scale.x(puntos.length - 1) + 7}" y="${scale.y(ultimo.v) + 4}">${num(ultimo.v)}</text>
      ${xTicks(puntos, scale)}
      <line class="cursor" y1="${CHART_PAD.top}" y2="${base}"/>
      <circle class="marker" r="5"/>
      <rect class="hit" x="0" y="0" width="${scale.width}" height="${CHART_H}"/>
    </svg>`;
  wireHover(el, el.querySelector("svg"), puntos, scale, (p) =>
    `<strong>${num(p.v)}</strong> cuentas<br>${periodLabel(p.periodo)}${p.altas ? ` · ${num(p.altas)} altas` : ""}`);

  const { base: b, panel } = state.eda;
  $("chart-base-legend").textContent =
    `${num(b.cuentas_activas_primero)} cuentas en ${periodLabel(panel.periodo_desde)} y ${num(b.cuentas_activas_ultimo)} en ` +
    `${periodLabel(panel.periodo_hasta)}: la base crece ${b.crecimiento_mensual.toFixed(2)}% por mes. ` +
    `Cualquier número en valores absolutos hay que leerlo contra este crecimiento.`;
}

function renderChurnChart() {
  const el = $("chart-churn");
  const puntos = state.eda.base.serie
    .filter((r) => r.churn_rate !== null)
    .map((r) => ({ ...r, v: r.churn_rate }));
  const promedio = state.eda.base.churn_rate_promedio;
  const max = Math.max(...puntos.map((p) => p.v), promedio);
  const scale = chartFrame(el, puntos.length, max, true);
  const base = CHART_PAD.top + scale.inner.h;

  // 4px de hueco entre barras contiguas: sin separacion la serie se lee como un bloque.
  const ancho = Math.max(4, scale.paso - 4);
  const barras = puntos.map((p, i) => {
    const y = scale.y(p.v);
    return `<rect class="serie-bar" x="${(scale.x(i) - ancho / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${ancho.toFixed(1)}" height="${(base - y).toFixed(1)}" rx="3"/>`;
  }).join("");

  const yRef = scale.y(promedio);
  const grid = [0, 0.5, 1].map((f) => {
    const y = scale.y(scale.top * f);
    return `<line class="grid-line" x1="${CHART_PAD.left}" x2="${scale.width - CHART_PAD.right}" y1="${y}" y2="${y}"/>
            <text class="axis-label" x="${CHART_PAD.left - 6}" y="${y + 3}" text-anchor="end">${(scale.top * f).toFixed(1)}%</text>`;
  }).join("");

  el.innerHTML = `
    <svg viewBox="0 0 ${scale.width} ${CHART_H}" role="img"
         aria-label="Churn rate mensual confirmado, promedio ${promedio.toFixed(2)} por ciento">
      ${grid}
      ${barras}
      <line class="ref-line" x1="${CHART_PAD.left}" x2="${scale.width - CHART_PAD.right}" y1="${yRef}" y2="${yRef}"/>
      <text class="ref-label" x="${scale.width - CHART_PAD.right + 4}" y="${yRef + 3}">${promedio.toFixed(2)}%</text>
      ${xTicks(puntos, scale)}
      <line class="cursor" y1="${CHART_PAD.top}" y2="${base}"/>
      <circle class="marker" r="5"/>
      <rect class="hit" x="0" y="0" width="${scale.width}" height="${CHART_H}"/>
    </svg>`;
  wireHover(el, el.querySelector("svg"), puntos, scale, (p) =>
    `<strong>${p.v.toFixed(2)}%</strong> de churn<br>${periodLabel(p.periodo)} · ${num(p.bajas_confirmadas)} de ${num(p.cuentas_evaluables)} cuentas`);

  const sin = state.eda.base.periodos_sin_etiqueta;
  $("chart-churn-legend").textContent =
    `La línea de puntos es el churn base: ${promedio.toFixed(2)}% mensual. Un modelo que no aporta nada acierta ` +
    `a ese ritmo, así que es el piso contra el que se compara. ` +
    (sin.length
      ? `Los últimos ${sin.length} meses no aparecen: para confirmar una baja hace falta ver los 3 meses siguientes.`
      : "");
}

/* ------------------------------------------------------- adopcion */

function renderAdopcion() {
  const { adopcion } = state.eda;
  $("adopcion-sub").textContent =
    `Porcentaje de las ${num(adopcion.cuentas)} cuentas activas de ${periodLabel(adopcion.periodo)} ` +
    `con rastro de cada funcionalidad en el mes.`;

  $("eda-adopcion").innerHTML = adopcion.grupos
    .map((g) => `
      <div class="adop-grupo">
        <h3>${escapeHtml(g.grupo)}</h3>
        ${g.items.map(adopRow).join("")}
      </div>`)
    .join("");
}

function adopRow(item) {
  const mediana = item.mediana_entre_usuarios === null
    ? ""
    : ` · mediana de ${num(item.mediana_entre_usuarios)} entre quienes la usan`;
  return `
    <div class="adop-row" title="${escapeHtml(item.etiqueta)}: ${num(item.cuentas)} cuentas${mediana}">
      <div class="adop-label">${escapeHtml(item.etiqueta)}</div>
      <div class="adop-track"><div class="adop-fill" style="width:${item.pct}%"></div></div>
      <div class="adop-pct">${item.pct.toFixed(0)}%</div>
    </div>`;
}

/* --------------------------------------------- se queda / churnea */

function renderSenales() {
  const { senales, n_churn, n_se_queda } = state.eda.churn;

  const leyenda = `
    <div class="senal-head">
      <span class="senal-key"><span class="senal-swatch" style="background:var(--chart-1)"></span>Se queda</span>
      <span class="senal-key"><span class="senal-swatch" style="background:var(--chart-2)"></span>Se da de baja</span>
      <span style="margin-left:auto">lift</span>
    </div>`;

  $("eda-senales").innerHTML = leyenda + senales.slice(0, 10).map(senalRow).join("");
  $("eda-senales-legend").textContent =
    `Sobre ${num(n_se_queda + n_churn)} meses-cuenta con etiqueta (${num(n_churn)} terminaron en baja). ` +
    `El lift es el cociente de adopción: 0.33 significa que la funcionalidad aparece un 67% menos seguido ` +
    `entre las cuentas que se van. Es descriptivo, no causal — una cuenta que ya dejó de operar deja de usar todo.`;
}

function senalRow(s) {
  const fuerte = s.lift !== null && s.lift < 0.8 ? " fuerte" : "";
  return `
    <div class="senal-row">
      <div class="senal-label">${escapeHtml(s.etiqueta)}</div>
      <div class="senal-bars">
        <div class="senal-bar">
          <div class="senal-track"><div class="senal-fill queda" style="width:${s.adopcion_se_queda}%"></div></div>
          <div class="senal-val">${s.adopcion_se_queda.toFixed(0)}%</div>
        </div>
        <div class="senal-bar">
          <div class="senal-track"><div class="senal-fill churn" style="width:${s.adopcion_churn}%"></div></div>
          <div class="senal-val">${s.adopcion_churn.toFixed(0)}%</div>
        </div>
      </div>
      <div class="senal-lift${fuerte}">${s.lift === null ? "—" : s.lift.toFixed(2)}</div>
    </div>`;
}

/* ------------------------------------------------- cortes de churn */

function renderCortes() {
  const { antiguedad } = state.eda.churn;
  const { paises } = state.eda.base;
  const max = Math.max(...antiguedad.map((a) => a.churn_rate), ...paises.map((p) => p.churn_rate || 0), 1);

  const fila = (etiqueta, valor, sub) => `
    <div class="corte-row" title="${escapeHtml(sub)}">
      <div class="corte-label">${escapeHtml(etiqueta)}</div>
      <div class="corte-track"><div class="corte-fill" style="width:${(valor / max) * 100}%"></div></div>
      <div class="corte-val">${valor.toFixed(2)}%</div>
    </div>`;

  const primero = antiguedad[0];
  const ultimo = antiguedad[antiguedad.length - 1];
  const veces = primero && ultimo && ultimo.churn_rate ? (primero.churn_rate / ultimo.churn_rate).toFixed(1) : null;

  $("eda-cortes").innerHTML = `
    <div class="corte-bloque">
      <h3>Por antigüedad de la cuenta</h3>
      ${antiguedad.map((a) => fila(a.tramo, a.churn_rate, `${num(a.cuentas)} meses-cuenta evaluados`)).join("")}
      ${veces ? `<p class="corte-nota">Una cuenta de menos de 3 meses se da de baja ${veces} veces más seguido que una de más de 4 años: el churn de Fudo es, sobre todo, un problema de los primeros meses.</p>` : ""}
    </div>
    <div class="corte-bloque">
      <h3>Por país</h3>
      ${paises.filter((p) => p.churn_rate !== null)
        .map((p) => fila(p.pais, p.churn_rate, `${num(p.cuentas)} cuentas · ${p.pct_base.toFixed(1)}% de la base`))
        .join("")}
    </div>`;
}

/* ---------------------------------------------------------------- tabla */

const SEASONAL_LABELS = { null: "Estacionales: todas", true: "Solo estacionales", false: "Sin estacionales" };

function renderRiskFilters() {
  const riesgo = ["alto", "medio", "bajo", "no_churn"]
    .map((k) => `<button class="chip ${state.risk.includes(k) ? "on" : ""}" data-risk="${k}">${RISK_LABELS[k]}</button>`)
    .join("");

  // Ciclo de tres estados: todas -> solo estacionales -> sin estacionales.
  const seasonal = `<button class="chip ${state.seasonal !== null ? "on" : ""}" data-seasonal="1"
    title="Las cuentas con patrón de pausas pueden estar cerrando por temporada, no dándose de baja.">
    ${SEASONAL_LABELS[String(state.seasonal)]}</button>`;

  $("risk-filters").innerHTML = riesgo + seasonal;

  $("risk-filters").querySelectorAll("[data-risk]").forEach((chip) => {
    chip.onclick = () => {
      const key = chip.dataset.risk;
      state.risk = state.risk.includes(key)
        ? state.risk.filter((r) => r !== key)
        : [...state.risk, key];
      state.page = 1;
      renderRiskFilters();
      loadAccounts();
    };
  });

  $("risk-filters").querySelector("[data-seasonal]").onclick = () => {
    state.seasonal = state.seasonal === null ? true : state.seasonal === true ? false : null;
    state.page = 1;
    renderRiskFilters();
    loadAccounts();
  };
}

function wireControls() {
  let timer;
  $("search").oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.search = e.target.value.trim();
      state.page = 1;
      loadAccounts();
    }, 260);
  };

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.onclick = () => {
      const key = th.dataset.sort;
      state.ascending = state.sortBy === key ? !state.ascending : false;
      state.sortBy = key;
      state.page = 1;
      loadAccounts();
    };
  });

  $("prev").onclick = () => { if (state.page > 1) { state.page--; loadAccounts(); } };
  $("next").onclick = () => { if (state.page < state.pages) { state.page++; loadAccounts(); } };

  $("drawer-close").onclick = closeDrawer;
  $("scrim").onclick = closeDrawer;
  document.onkeydown = (e) => { if (e.key === "Escape") closeDrawer(); };
}

async function loadAccounts() {
  const data = await api("/api/v1/accounts", {
    risk: state.risk,
    seasonal: state.seasonal === null ? undefined : state.seasonal,
    search: state.search,
    sort_by: state.sortBy,
    ascending: state.ascending,
    page: state.page,
    size: state.size,
  });

  state.pages = data.pages;
  $("page-info").textContent =
    `${num(data.total)} cuentas · página ${data.page} de ${data.pages}`;
  $("prev").disabled = data.page <= 1;
  $("next").disabled = data.page >= data.pages;

  if (!data.items.length) {
    $("accounts-body").innerHTML =
      `<tr><td colspan="6" class="empty">No hay cuentas que cumplan el filtro.</td></tr>`;
    return;
  }

  $("accounts-body").innerHTML = data.items.map(rowHtml).join("");
  $("accounts-body").querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.onclick = () => openDrawer(Number(tr.dataset.id));
  });
}

function rowHtml(a) {
  const color = RISK_COLORS[a.risk_category];
  return `
    <tr data-id="${a.id}">
      <td>
        <span class="acct-name" title="${escapeHtml(a.nombre || "")}">${escapeHtml(a.nombre || "(sin nombre)")}</span>
        <span class="acct-id">#${a.id}</span>
      </td>
      <td><span class="mono">${escapeHtml(a.plan || "—")}</span></td>
      <td class="num">
        <span class="prob">
          <span class="prob-track"><span class="prob-fill" style="width:${a.churn_probability * 100}%;background:${color}"></span></span>
          ${pct(a.churn_probability, 1)}
        </span>
      </td>
      <td class="num">${money(a.monthly_revenue)}</td>
      <td class="num"><strong>${money(a.revenue_at_risk)}</strong></td>
      <td>
        <span class="tag tag-${a.risk_category}">${RISK_LABELS[a.risk_category]}</span>
        ${a.posible_estacional ? '<span class="tag tag-estacional" title="Patrón de pausas de temporada: su baja puede ser un cierre estacional.">estacional</span>' : ""}
      </td>
    </tr>`;
}

/* ------------------------------------------------------ detalle (drawer) */

async function openDrawer(accountId) {
  $("drawer").classList.add("on");
  $("scrim").classList.add("on");
  $("drawer-content").innerHTML = `
    <div class="skeleton" style="height:28px;width:60%"></div>
    <div class="skeleton" style="height:88px;margin-top:20px"></div>
    <div class="skeleton" style="height:200px;margin-top:20px"></div>`;

  try {
    const a = await api(`/api/v1/accounts/${accountId}`);
    $("drawer-content").innerHTML = detailHtml(a);
  } catch (error) {
    $("drawer-content").innerHTML = `<p class="empty">No se pudo cargar la cuenta: ${error.message}</p>`;
  }
}

function closeDrawer() {
  $("drawer").classList.remove("on");
  $("scrim").classList.remove("on");
}


/** Contexto para CX: si la cuenta ya pausó antes, su baja puede ser un cierre de
 *  temporada y no una pérdida. El modelo no lo sabe — esto viene del historial. */
function avisoEstacional(a) {
  if (!a.posible_estacional) return "";
  const mes = a.mes_de_pausa_habitual
    ? ` Suele pausar en ${MESES[a.mes_de_pausa_habitual - 1]}.`
    : "";
  const veces = a.pausas_historicas === 1 ? "una vez" : `${a.pausas_historicas} veces`;
  return `
    <div class="aviso-estacional">
      <span>◷</span>
      <div><strong>Posible negocio de temporada.</strong> Esta cuenta ya se dio de baja y
      volvió ${veces}.${mes} Su desaparición puede ser un cierre estacional y no una
      pérdida: conviene confirmarlo antes de gastar una acción de retención.</div>
    </div>`;
}

function detailHtml(a) {
  const heat = (a.top_features || []).map(heatRow).join("");
  const plan = planHtml(a);

  return `
    <div class="d-head">
      <h2>${escapeHtml(a.nombre || "(sin nombre)")}</h2>
      <p class="d-sub">
        Cuenta #${a.id} · ${escapeHtml(a.pais || "—")} · estado ${escapeHtml(a.estado || "—")}
        <span class="tag tag-${a.risk_category}" style="margin-left:8px">${RISK_LABELS[a.risk_category]}</span>
      </p>
    </div>

    <div class="d-stats">
      <div class="d-stat">
        <div class="d-stat-label">Prob. de baja</div>
        <div class="d-stat-value" style="color:${RISK_COLORS[a.risk_category]}">${pct(a.churn_probability, 1)}</div>
      </div>
      <div class="d-stat">
        <div class="d-stat-label">Revenue / mes</div>
        <div class="d-stat-value">${money(a.monthly_revenue)}</div>
      </div>
      <div class="d-stat">
        <div class="d-stat-label">En riesgo</div>
        <div class="d-stat-value">${money(a.revenue_at_risk)}</div>
      </div>
    </div>

    ${avisoEstacional(a)}
    ${a.diagnostico ? `<div class="d-narrative">${escapeHtml(a.diagnostico)}</div>` : ""}

    <div class="d-section-title">Qué explica esta predicción</div>
    <div class="heat">${heat || '<p class="empty">Sin contribuciones registradas.</p>'}</div>

    <div class="d-section-title">Plan y revenue</div>
    ${plan}`;
}

function heatRow(c) {
  const color = heatColor(c);
  const percentile = c.percentile === null || c.percentile === undefined ? null : c.percentile;
  const width = percentile === null ? 0 : percentile;
  const value = c.value === null || c.value === undefined ? "—" : c.value;

  const meta = percentile === null
    ? `valor ${value}`
    : `valor ${value} · percentil ${percentile.toFixed(0)} de la base`;

  return `
    <div class="heat-row" style="--heat:${color}">
      <div>
        <div class="heat-label">${escapeHtml(c.label)}</div>
        <div class="heat-meta">${escapeHtml(meta)} · ${c.direction} el riesgo</div>
      </div>
      <div class="heat-bar-wrap" title="La barra es el percentil de la cuenta; el número, el aporte al riesgo (SHAP).">
        <div class="heat-track"><div class="heat-fill" style="width:${width}%;background:${color}"></div></div>
        <div class="heat-val">${c.shap_value > 0 ? "+" : ""}${c.shap_value.toFixed(2)}</div>
      </div>
    </div>`;
}

function planHtml(a) {
  let items = [];
  try {
    items = JSON.parse(a.revenue_desglose || "[]");
  } catch (_) { /* el desglose es opcional */ }

  const rows = items
    .map((i) => `
      <div class="d-plan-row">
        <span>${i.kind === "plan" ? "Plan" : "Módulo"} <span class="mono">${escapeHtml(i.code)}</span></span>
        <span>${i.price === null ? "sin precio" : money(i.price)}</span>
      </div>`)
    .join("");

  return `
    <div class="d-plan">
      <div class="d-plan-row"><span>${escapeHtml(a.plan_descripcion || a.plan || "—")}</span><span></span></div>
      ${rows}
      <div class="d-plan-total">
        <span>Total mensual</span>
        <span>${money(a.monthly_revenue)} ${state.currency}</span>
      </div>
    </div>`;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

boot();
